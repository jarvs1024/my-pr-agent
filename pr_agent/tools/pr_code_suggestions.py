import asyncio
import copy
import difflib
import re
import textwrap
import traceback
from datetime import datetime
from functools import partial
from typing import Dict, List, Optional

from jinja2 import Environment, StrictUndefined

from pr_agent.algo import MAX_TOKENS
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.git_patch_processing import decouple_and_convert_to_hunks_with_lines_numbers
from pr_agent.algo.repo_context import build_repo_context, extract_rule_keys
from pr_agent.algo.improve_coverage import compute_uncovered_rules, render_uncovered_details
from pr_agent.telemetry import events as telemetry_events
from pr_agent.algo.pr_processing import (add_ai_metadata_to_diff_files,
                                         get_pr_diff, get_pr_multi_diffs,
                                         retry_with_fallback_models)
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.i18n import t
from pr_agent.algo.utils import (ModelType, load_yaml, replace_code_tags, format_exception_chain,
                                 show_relevant_configurations, get_max_tokens, clip_tokens, get_model)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import (AzureDevopsProvider, GithubProvider,
                                    GitLabProvider, get_git_provider,
                                    get_git_provider_with_context)
from pr_agent.git_providers.git_provider import get_main_pr_language, GitProvider
from pr_agent.log import get_logger
from pr_agent.servers.help import HelpMessage
from pr_agent.tools.pr_description import insert_br_after_x_chars


class PRCodeSuggestions:
    def __init__(self, pr_url: str, cli_mode=False, args: list = None,
                 ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):

        self.git_provider = get_git_provider_with_context(pr_url)
        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )

        num_code_suggestions = int(get_settings().pr_code_suggestions.num_code_suggestions_per_chunk)

        self.ai_handler = ai_handler()
        self.ai_handler.main_pr_language = self.main_language
        self.patches_diff = None
        self.prediction = None
        self.pr_url = pr_url
        self.cli_mode = cli_mode
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True))
        if (self.pr_description_files and get_settings().get("config.is_auto_command", False) and
                get_settings().get("config.enable_ai_metadata", False)):
            add_ai_metadata_to_diff_files(self.git_provider, self.pr_description_files)
            get_logger().debug(f"AI metadata added to the this command")
        else:
            get_settings().set("config.enable_ai_metadata", False)
            get_logger().debug(f"AI metadata is disabled for this command")

        _repo_context_for_vars = build_repo_context(self.git_provider)

        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",  # empty diff for initial calculation
            "diff_no_line_numbers": "",  # empty diff for initial calculation
            "num_code_suggestions": num_code_suggestions,
            "extra_instructions": get_settings().pr_code_suggestions.extra_instructions,
            "repo_context": _repo_context_for_vars,
            "agents_md_rules": extract_rule_keys(_repo_context_for_vars),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "relevant_best_practices": "",
            "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
            "focus_only_on_problems": get_settings().get("pr_code_suggestions.focus_only_on_problems", False),
            "date": datetime.now().strftime('%Y-%m-%d'),
            'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
        }

        if get_settings().pr_code_suggestions.get("decouple_hunks", True):
            self.pr_code_suggestions_prompt_system = get_settings().pr_code_suggestions_prompt.system
            self.pr_code_suggestions_prompt_user = get_settings().pr_code_suggestions_prompt.user
        else:
            self.pr_code_suggestions_prompt_system = get_settings().pr_code_suggestions_prompt_not_decoupled.system
            self.pr_code_suggestions_prompt_user = get_settings().pr_code_suggestions_prompt_not_decoupled.user

        self.token_handler = TokenHandler(self.git_provider.pr,
                                          self.vars,
                                          self.pr_code_suggestions_prompt_system,
                                          self.pr_code_suggestions_prompt_user)

        self.progress = (
            t("pr_code_suggestions.thinking", "## Generating PR code suggestions\n\n\nWork in progress ...<br>\n<img src=\"https://codium.ai/images/pr_agent/dual_ball_loading-crop.gif\" width=48>")
        )
        self.progress_response = None

    async def run(self):
        # Telemetry: track this /improve run from start to finish
        import time
        _run_id = None
        _run_started_at = time.monotonic()
        try:
            _gp = self.git_provider
            _mr_id = (
                getattr(_gp, "id_mr", None)
                or getattr(getattr(_gp, "pr", None), "iid", None)
                or 0
            )
            _raw_pid = getattr(_gp, "id_project", None) or 0
            _pid = _raw_pid if isinstance(_raw_pid, int) else 0
            if not _pid and isinstance(_raw_pid, str):
                try:
                    _pid = _gp.gl.projects.get(_raw_pid).id
                except Exception:
                    _pid = 0
            _run_id = telemetry_events.emit_run_started(
                mr_id=int(_mr_id or 0),
                project_id=int(_pid or 0),
                command="improve",
                triggered_by=getattr(get_settings().config, "is_auto_command", False) and "auto" or "user",
                model=getattr(get_settings().config, "model", None),
            )
        except Exception:
            _run_id = None
        _run_status = {"name": "empty", "suggestion_count": 0, "rule_keys": []}
        try:
            if not self.git_provider.get_files():
                get_logger().info(f"PR has no files: {self.pr_url}, skipping code suggestions")
                return None

            get_logger().info('Generating code suggestions for PR...')
            relevant_configs = {'pr_code_suggestions': dict(get_settings().pr_code_suggestions),
                                'config': dict(get_settings().config)}
            get_logger().debug("Relevant configs", artifacts=relevant_configs)

            # publish "Preparing suggestions..." comments
            if (get_settings().config.publish_output and get_settings().config.publish_output_progress and
                    not get_settings().config.get('is_auto_command', False)):
                if self.git_provider.is_supported("gfm_markdown"):
                    self.progress_response = self.git_provider.publish_comment(self.progress)
                else:
                    self.git_provider.publish_comment(t("pr_code_suggestions.thinking", "Preparing suggestions..."), is_temporary=True)

            # # call the model to get the suggestions, and self-reflect on them
            # if not self.is_extended:
            #     data = await retry_with_fallback_models(self._prepare_prediction, model_type=ModelType.REGULAR)
            # else:
            data = await retry_with_fallback_models(self.prepare_prediction_main, model_type=ModelType.REGULAR)
            if not data:
                data = {"code_suggestions": []}
            self.data = data

            # Handle the case where the PR has no suggestions
            if (data is None or 'code_suggestions' not in data or not data['code_suggestions']):
                await self.publish_no_suggestions()
                return

            # publish the suggestions
            if get_settings().config.publish_output:
                # If a temporary comment was published, remove it
                self.git_provider.remove_initial_comment()

                # Publish table summarized suggestions
                if ((not get_settings().pr_code_suggestions.commitable_code_suggestions) and
                        self.git_provider.is_supported("gfm_markdown")):

                    # generate summarized suggestions
                    pr_body = self.generate_summarized_suggestions(data)
                    get_logger().debug(f"PR output", artifact=pr_body)

                    # require self-review
                    if get_settings().pr_code_suggestions.demand_code_suggestions_self_review:
                        pr_body = await self.add_self_review_text(pr_body)

                    # add usage guide
                    if (get_settings().pr_code_suggestions.enable_chat_text and get_settings().config.is_auto_command
                            and isinstance(self.git_provider, GithubProvider)):
                        pr_body += "\n\n>💡 Need additional feedback ? start a [PR chat](https://chromewebstore.google.com/detail/ephlnjeghhogofkifjloamocljapahnl) \n\n"
                    if get_settings().pr_code_suggestions.enable_help_text:
                        pr_body += "<hr>\n\n<details> <summary><strong>💡 Tool usage guide:</strong></summary><hr> \n\n"
                        pr_body += HelpMessage.get_improve_usage_guide()
                        pr_body += "\n</details>\n"

                    # Surface AGENTS.md rule coverage gap so reviewers see which rule_keys
                    # the LLM silently dropped. Computed from the same `data` we just
                    # received; relies on `agents_md_rules` already injected into `self.vars`.
                    _required_rules = self.vars.get("agents_md_rules") or []
                    _uncovered = compute_uncovered_rules(_required_rules, data.get("code_suggestions") or [])
                    _total_required = len(self.vars.get("agents_md_rules") or [])
                    pr_body += render_uncovered_details(_uncovered, total_required=_total_required)

                    # Output the relevant configurations if enabled
                    if get_settings().get('config', {}).get('output_relevant_configurations', False):
                        pr_body += show_relevant_configurations(relevant_section='pr_code_suggestions')

                    # publish the PR comment
                    if get_settings().pr_code_suggestions.persistent_comment: # true by default
                        self.publish_persistent_comment_with_history(self.git_provider,
                                                                     pr_body,
                                                                     initial_header=t("pr_code_suggestions.header", "## PR Code Suggestions ✨"),
                                                                     update_header=True,
                                                                     name="suggestions",
                                                                     final_update_message=False,
                                                                     max_previous_comments=get_settings().pr_code_suggestions.max_history_len,
                                                                     progress_response=self.progress_response)
                    else:
                        if self.progress_response:
                            self.git_provider.edit_comment(self.progress_response, body=pr_body)
                        else:
                            self.git_provider.publish_comment(pr_body)

                    # dual publishing mode
                    if int(get_settings().pr_code_suggestions.dual_publishing_score_threshold) > 0:
                        await self.dual_publishing(data)
                else:
                    await self.push_inline_code_suggestions(data)
                    if self.progress_response:
                        self.git_provider.remove_comment(self.progress_response)
                    _run_status["name"] = "success"
                    _run_status["suggestion_count"] = len(data.get("code_suggestions") or [])
                    _run_status["rule_keys"] = sorted({k for cs in (data.get("code_suggestions") or []) for k in telemetry_events.extract_rule_keys_from_text(
                        str(cs.get("suggestion_content") or "") + " " + str(cs.get("one_sentence_summary") or "")
                    )})

                    # Inline-only path has no persistent review body for the coverage
                    # checklist. Decide what to post based on what the pipeline
                    # actually did with the LLM output:
                    #   1. LLM emitted 0 suggestions -> "no suggestions found"
                    #   2. LLM emitted N but pipeline suppressed all of them
                    #      -> tell the reviewer explicitly which lines were
                    #         skipped, so they don't mistake silence for a
                    #         clean review.
                    #   3. Otherwise -> standard rule-coverage checklist.
                    _outcome = getattr(self, '_last_suggestion_outcome', None) or {}
                    if get_settings().config.publish_output:
                        if (_outcome.get('llm_emitted', 0) > 0
                                and _outcome.get('kept', 0) == 0
                                and _outcome.get('suppressed_count', 0) > 0):
                            _sup_lines = _outcome.get('suppressed_lines') or []
                            _shown = _sup_lines[:5]
                            _more = len(_sup_lines) - len(_shown)
                            _lines_str = ", ".join(
                                f"{fn.rsplit('/', 1)[-1]}:L{ln}"
                                for fn, ln, _ in _shown
                            )
                            _more_str = f" 等 {len(_sup_lines)} 处" if _more > 0 else ""
                            _body = (
                                "本次 `/improve` 生成 **" + str(len(_sup_lines)) + "** 条建议，"
                                " 但都匹配到已 resolve 的位置或同规则行号漂移记录，已自动跳过：\n\n"
                                "- " + _lines_str + _more_str + "\n\n"
                                "如果这些行仍然有问题，可手动修改源文件后再跑 `/improve`，"
                                " 或先用 `/suggestion_status` 看 telemetry 状态。"
                            )
                            self.git_provider.publish_comment(_body)
                        else:
                            _inline_uncovered = compute_uncovered_rules(
                                self.vars.get("agents_md_rules") or [],
                                data.get("code_suggestions") or [],
                            )
                            _inline_total = len(self.vars.get("agents_md_rules") or [])
                            _inline_details = render_uncovered_details(
                                _inline_uncovered, total_required=_inline_total,
                            )
                            if _inline_details:
                                self.git_provider.publish_comment(_inline_details)
            else:
                get_logger().info('Code suggestions generated for PR, but not published since publish_output is False.')
                pr_body = self.generate_summarized_suggestions(data)
                get_settings().data = {"artifact": pr_body}
                return
        except Exception as e:
            get_logger().error("Failed to generate code suggestions for PR, error: %s", format_exception_chain(e),
                               artifact={"traceback": traceback.format_exc()})
            _run_status["name"] = "failed"
            # Use the shared ``format_exception_chain`` helper so the captured
            # error text in telemetry shows the *root* cause (rate limit, auth,
            # timeout) and not just the outer wrapper like
            # ``Failed to generate prediction with any model of [...]``.
            _run_status["error"] = format_exception_chain(e)
            if get_settings().config.publish_output:
                if self.progress_response:
                    self.git_provider.remove_comment(self.progress_response)
                else:
                    try:
                        self.git_provider.remove_initial_comment()
                        # Flat notification only — the full error chain is persisted
                        # to telemetry.review_runs.error (data collection) and shown
                        # in the dashboard. MR comments stay short so the thread isn't
                        # polluted with internal stack traces.
                        self.git_provider.publish_comment(
                            t("pr_code_suggestions.failed",
                              "Failed to generate code suggestions for PR")
                        )
                    except Exception as e:
                        get_logger().exception("Failed to update persistent review, error: %s", format_exception_chain(e))
        finally:
            if _run_id:
                try:
                    _gp_final = self.git_provider
                    _final_mr = (
                        getattr(_gp_final, "id_mr", None)
                        or getattr(getattr(_gp_final, "pr", None), "iid", None)
                        or 0
                    )
                    _raw_pid = getattr(_gp_final, "id_project", None) or 0
                    _final_pid = _raw_pid if isinstance(_raw_pid, int) else 0
                    if not _final_pid and isinstance(_raw_pid, str):
                        try:
                            _final_pid = _gp_final.gl.projects.get(_raw_pid).id
                        except Exception:
                            _final_pid = 0
                except Exception:
                    _final_mr = 0
                    _final_pid = 0
                telemetry_events.emit_run_finished(
                    _run_id,
                    status=_run_status["name"],
                    suggestion_count=_run_status.get("suggestion_count", 0),
                    rule_keys=_run_status.get("rule_keys", []),
                    error=_run_status.get("error"),
                    duration_ms=int((time.monotonic() - _run_started_at) * 1000),
                    mr_id=int(_final_mr or 0),
                    project_id=int(_final_pid or 0),
                    command="improve",
                )

    async def add_self_review_text(self, pr_body):
        text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
        pr_body += f"\n\n- [ ]  {text}"
        approve_pr_on_self_review = get_settings().pr_code_suggestions.approve_pr_on_self_review
        fold_suggestions_on_self_review = get_settings().pr_code_suggestions.fold_suggestions_on_self_review
        if approve_pr_on_self_review and not fold_suggestions_on_self_review:
            pr_body += ' <!-- approve pr self-review -->'
        elif fold_suggestions_on_self_review and not approve_pr_on_self_review:
            pr_body += ' <!-- fold suggestions self-review -->'
        else:
            pr_body += ' <!-- approve and fold suggestions self-review -->'
        return pr_body

    async def publish_no_suggestions(self):
        pr_body = (
            t("pr_code_suggestions.header", "## PR Code Suggestions ✨") + "\n\n"
            + t("pr_code_suggestions.no_suggestions", "No code suggestions found for the PR.")
        )
        # Surface AGENTS.md rule coverage gap when /improve came back empty — useful for
        # the "LLM produced no suggestions" branch which has no inline markers to inspect.
        _uncovered = compute_uncovered_rules(self.vars.get("agents_md_rules") or [], [])
        _total_required = len(self.vars.get("agents_md_rules") or [])
        pr_body += render_uncovered_details(_uncovered, total_required=_total_required)
        if (get_settings().config.publish_output and
                get_settings().pr_code_suggestions.get('publish_output_no_suggestions', True)):
            get_logger().warning('No code suggestions found for the PR.')
            get_logger().debug(f"PR output", artifact=pr_body)
            if self.progress_response:
                self.git_provider.edit_comment(self.progress_response, body=pr_body)
            else:
                self.git_provider.publish_comment(pr_body)
        else:
            get_settings().data = {"artifact": ""}

    async def dual_publishing(self, data):
        data_above_threshold = {'code_suggestions': []}
        try:
            for suggestion in data['code_suggestions']:
                if int(suggestion.get('score', 0)) >= int(
                        get_settings().pr_code_suggestions.dual_publishing_score_threshold) \
                        and suggestion.get('improved_code'):
                    data_above_threshold['code_suggestions'].append(suggestion)
                    if not data_above_threshold['code_suggestions'][-1]['existing_code']:
                        get_logger().info(f'Identical existing and improved code for dual publishing found')
                        data_above_threshold['code_suggestions'][-1]['existing_code'] = suggestion[
                            'improved_code']
            if data_above_threshold['code_suggestions']:
                get_logger().info(
                    f"Publishing {len(data_above_threshold['code_suggestions'])} suggestions in dual publishing mode")
                await self.push_inline_code_suggestions(data_above_threshold)
                _run_status["name"] = "success"
                _run_status["suggestion_count"] = len(data_above_threshold.get("code_suggestions") or [])
                _run_status["rule_keys"] = sorted({k for cs in (data_above_threshold.get("code_suggestions") or []) for k in telemetry_events.extract_rule_keys_from_text(
                    str(cs.get("suggestion_content") or "") + " " + str(cs.get("one_sentence_summary") or "")
                )})
        except Exception as e:
            get_logger().error("Failed to publish dual publishing suggestions, error: %s", format_exception_chain(e))

    @staticmethod
    def publish_persistent_comment_with_history(git_provider: GitProvider,
                                                pr_comment: str,
                                                initial_header: str,
                                                update_header: bool = True,
                                                name='review',
                                                final_update_message=True,
                                                max_previous_comments=4,
                                                progress_response=None,
                                                only_fold=False):

        def _extract_link(comment_text: str):
            r = re.compile(r"<!--.*?-->")
            match = r.search(comment_text)

            up_to_commit_txt = ""
            if match:
                up_to_commit_txt = f" up to commit {match.group(0)[4:-3].strip()}"
            return up_to_commit_txt

        history_header = f"#### Previous suggestions\n"
        last_commit_num = git_provider.get_latest_commit_url().split('/')[-1][:7]
        if only_fold: # A user clicked on the 'self-review' checkbox
            text = get_settings().pr_code_suggestions.code_suggestions_self_review_text
            latest_suggestion_header = f"\n\n- [x]  {text}"
        else:
            latest_suggestion_header = f"Latest suggestions up to {last_commit_num}"
        latest_commit_html_comment = f"<!-- {last_commit_num} -->"
        found_comment = None

        if max_previous_comments > 0:
            try:
                prev_comments = list(git_provider.get_issue_comments())
                for comment in prev_comments:
                    if comment.body.startswith(initial_header):
                        prev_suggestions = comment.body
                        found_comment = comment
                        comment_url = git_provider.get_comment_url(comment)

                        if history_header.strip() not in comment.body:
                            # no history section
                            # extract everything between <table> and </table> in comment.body including <table> and </table>
                            table_index = comment.body.find("<table>")
                            if table_index == -1:
                                git_provider.edit_comment(comment, pr_comment)
                                continue
                            # find http link from comment.body[:table_index]
                            up_to_commit_txt = _extract_link(comment.body[:table_index])
                            prev_suggestion_table = comment.body[
                                                    table_index:comment.body.rfind("</table>") + len("</table>")]

                            tick = "✅ " if "✅" in prev_suggestion_table else ""
                            # surround with details tag
                            prev_suggestion_table = f"<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n<br>{prev_suggestion_table}\n\n</details>"

                            new_suggestion_table = pr_comment.replace(initial_header, "").strip()

                            pr_comment_updated = f"{initial_header}\n{latest_commit_html_comment}\n\n"
                            pr_comment_updated += f"{latest_suggestion_header}\n{new_suggestion_table}\n\n___\n\n"
                            pr_comment_updated += f"{history_header}{prev_suggestion_table}\n"
                        else:
                            # get the text of the previous suggestions until the latest commit
                            sections = prev_suggestions.split(history_header.strip())
                            latest_table = sections[0].strip()
                            prev_suggestion_table = sections[1].replace(history_header, "").strip()

                            # get text after the latest_suggestion_header in comment.body
                            table_ind = latest_table.find("<table>")
                            up_to_commit_txt = _extract_link(latest_table[:table_ind])

                            latest_table = latest_table[table_ind:latest_table.rfind("</table>") + len("</table>")]
                            # enforce max_previous_comments
                            count = prev_suggestions.count(f"\n<details><summary>{name.capitalize()}")
                            count += prev_suggestions.count(f"\n<details><summary>✅ {name.capitalize()}")
                            if count >= max_previous_comments:
                                # remove the oldest suggestion
                                prev_suggestion_table = prev_suggestion_table[:prev_suggestion_table.rfind(
                                    f"<details><summary>{name.capitalize()} up to commit")]

                            tick = "✅ " if "✅" in latest_table else ""
                            # Add to the prev_suggestions section
                            last_prev_table = f"\n<details><summary>{tick}{name.capitalize()}{up_to_commit_txt}</summary>\n<br>{latest_table}\n\n</details>"
                            prev_suggestion_table = last_prev_table + "\n" + prev_suggestion_table

                            new_suggestion_table = pr_comment.replace(initial_header, "").strip()

                            pr_comment_updated = f"{initial_header}\n"
                            pr_comment_updated += f"{latest_commit_html_comment}\n\n"
                            pr_comment_updated += f"{latest_suggestion_header}\n\n{new_suggestion_table}\n\n"
                            pr_comment_updated += "___\n\n"
                            pr_comment_updated += f"{history_header}\n"
                            pr_comment_updated += f"{prev_suggestion_table}\n"

                        get_logger().info(f"Persistent mode - updating comment {comment_url} to latest {name} message")
                        if progress_response:  # publish to 'progress_response' comment, because it refreshes immediately
                            git_provider.edit_comment(progress_response, pr_comment_updated)
                            git_provider.remove_comment(comment)
                            comment = progress_response
                        else:
                            git_provider.edit_comment(comment, pr_comment_updated)
                        return comment
            except Exception as e:
                # Surface the full __cause__ chain so the log shows whether the
                # failure is a network timeout, GitLab 5xx, auth, etc. — the bare
                # "Failed to update persistent review" message is too generic.
                _err = e
                _chain = [f"{type(_err).__name__}: {_err}"]
                while _err.__cause__ is not None:
                    _err = _err.__cause__
                    _chain.append(f"{type(_err).__name__}: {_err}")
                get_logger().exception(
                    f"Failed to update persistent review, error: {' -> '.join(_chain)}"
                )
                pass

        # if we are here, we did not find a previous comment to update
        body = pr_comment.replace(initial_header, "").strip()
        pr_comment = f"{initial_header}\n\n{latest_commit_html_comment}\n\n{body}\n\n"
        if progress_response:
            git_provider.edit_comment(progress_response, pr_comment)
            new_comment = progress_response
        else:
            new_comment = git_provider.publish_comment(pr_comment)
        return new_comment


    def extract_link(self, s):
        r = re.compile(r"<!--.*?-->")
        match = r.search(s)

        up_to_commit_txt = ""
        if match:
            up_to_commit_txt = f" up to commit {match.group(0)[4:-3].strip()}"
        return up_to_commit_txt

    async def _prepare_prediction(self, model: str) -> dict:
        self.patches_diff = get_pr_diff(self.git_provider,
                                        self.token_handler,
                                        model,
                                        add_line_numbers_to_hunks=True,
                                        disable_extra_lines=False)
        self.patches_diff_list = [self.patches_diff]
        self.patches_diff_no_line_number = self.remove_line_numbers([self.patches_diff])[0]

        if self.patches_diff:
            get_logger().debug(f"PR diff", artifact=self.patches_diff)
            self.prediction = await self._get_prediction(model, self.patches_diff, self.patches_diff_no_line_number)
        else:
            get_logger().warning(f"Empty PR diff")
            self.prediction = None

        data = self.prediction
        return data

    async def _get_prediction(self, model: str, patches_diff: str, patches_diff_no_line_number: str) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = patches_diff  # update diff
        variables["diff_no_line_numbers"] = patches_diff_no_line_number  # update diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(self.pr_code_suggestions_prompt_system).render(variables)
        user_prompt = environment.from_string(get_settings().pr_code_suggestions_prompt.user).render(variables)
        # Hard timeout: ``config.ai_timeout`` is passed to litellm but a slow
        # reasoning model can still hang forever on a connection that never
        # resets.  Wrap the call in ``asyncio.wait_for`` so the worst case is a
        # clean TimeoutError that the outer ``except`` block can convert into a
        # ``status=failed`` telemetry row instead of a permanently ``started``
        # row that never closes.
        try:
            import asyncio as _asyncio
            _ai_timeout = float(get_settings().config.ai_timeout or 120)
        except Exception:
            _ai_timeout = 120.0
        response, finish_reason = await _asyncio.wait_for(
            self.ai_handler.chat_completion(
                model=model, temperature=get_settings().config.temperature,
                system=system_prompt, user=user_prompt),
            timeout=_ai_timeout,
        )
        if not get_settings().config.publish_output:
            get_settings().system_prompt = system_prompt
            get_settings().user_prompt = user_prompt

        # load suggestions from the AI response
        data = self._prepare_pr_code_suggestions(response)

        # self-reflect on suggestions (mandatory, since line numbers are generated now here)
        model_reflect_with_reasoning = get_model('model_reasoning')
        fallbacks = get_settings().config.fallback_models
        if model_reflect_with_reasoning == get_settings().config.model and model != get_settings().config.model and fallbacks and model == \
                fallbacks[0]:
            # we are using a fallback model (should not happen on regular conditions)
            get_logger().warning(f"Using the same model for self-reflection as the one used for suggestions")
            model_reflect_with_reasoning = model
        response_reflect = await self.self_reflect_on_suggestions(data["code_suggestions"],
                                                                  patches_diff, model=model_reflect_with_reasoning)
        if response_reflect:
            await self.analyze_self_reflection_response(data, response_reflect)
        else:
            # get_logger().error(f"Could not self-reflect on suggestions. using default score 7")
            for i, suggestion in enumerate(data["code_suggestions"]):
                suggestion["score"] = 7
                suggestion["score_why"] = ""

        return data

    async def analyze_self_reflection_response(self, data, response_reflect):
        response_reflect_yaml = load_yaml(response_reflect)
        code_suggestions_feedback = response_reflect_yaml.get("code_suggestions", [])
        if code_suggestions_feedback and len(code_suggestions_feedback) == len(data["code_suggestions"]):
            for i, suggestion in enumerate(data["code_suggestions"]):
                try:
                    suggestion["score"] = code_suggestions_feedback[i]["suggestion_score"]
                    suggestion["score_why"] = code_suggestions_feedback[i]["why"]

                    if 'relevant_lines_start' not in suggestion:
                        relevant_lines_start = code_suggestions_feedback[i].get('relevant_lines_start', -1)
                        relevant_lines_end = code_suggestions_feedback[i].get('relevant_lines_end', -1)
                        suggestion['relevant_lines_start'] = relevant_lines_start
                        suggestion['relevant_lines_end'] = relevant_lines_end
                        if relevant_lines_start < 0 or relevant_lines_end < 0:
                            suggestion["score"] = 0

                    try:
                        if get_settings().config.publish_output:
                            if not suggestion["score"]:
                                score = -1
                            else:
                                score = int(suggestion["score"])
                            label = suggestion["label"].lower().strip()
                            label = label.replace('<br>', ' ')
                            suggestion_statistics_dict = {'score': score,
                                                          'label': label}
                            get_logger().info(f"PR-Agent suggestions statistics",
                                              statistics=suggestion_statistics_dict, analytics=True)
                    except Exception as e:
                        get_logger().error("Failed to log suggestion statistics, error: %s", format_exception_chain(e))
                        pass

                except Exception as e:  #
                    get_logger().error(f"Error processing suggestion score {i}",
                                       artifact={"suggestion": suggestion,
                                                 "code_suggestions_feedback": code_suggestions_feedback[i]})
                    suggestion["score"] = 7
                    suggestion["score_why"] = ""

                suggestion = self.validate_one_liner_suggestion_not_repeating_code(suggestion)

                # if the before and after code is the same, clear one of them
                try:
                    if suggestion['existing_code'] == suggestion['improved_code']:
                        get_logger().debug(
                            f"edited improved suggestion {i + 1}, because equal to existing code: {suggestion['existing_code']}")
                        if get_settings().pr_code_suggestions.commitable_code_suggestions:
                            suggestion['improved_code'] = ""  # we need 'existing_code' to locate the code in the PR
                        else:
                            suggestion['existing_code'] = ""
                except Exception as e:
                    get_logger().error("Error processing suggestion {i + 1}, error: %s", format_exception_chain(e))

    @staticmethod
    def _truncate_if_needed(suggestion):
        max_code_suggestion_length = get_settings().get("PR_CODE_SUGGESTIONS.MAX_CODE_SUGGESTION_LENGTH", 0)
        suggestion_truncation_message = get_settings().get("PR_CODE_SUGGESTIONS.SUGGESTION_TRUNCATION_MESSAGE", "")
        if max_code_suggestion_length > 0:
            if len(suggestion['improved_code']) > max_code_suggestion_length:
                get_logger().info(f"Truncated suggestion from {len(suggestion['improved_code'])} "
                                  f"characters to {max_code_suggestion_length} characters")
                suggestion['improved_code'] = suggestion['improved_code'][:max_code_suggestion_length]
                suggestion['improved_code'] += f"\n{suggestion_truncation_message}"
        return suggestion

    def _prepare_pr_code_suggestions(self, predictions: Optional[str]) -> Dict:
        if not predictions:
            # chat_completion returned None / aborted / empty — emit a clean empty
            # payload so callers don't crash with 'NoneType' errors.
            return {"code_suggestions": []}
        data = load_yaml(predictions.strip(),
                         keys_fix_yaml=["relevant_file", "suggestion_content", "existing_code", "improved_code"],
                         first_key="code_suggestions", last_key="label")
        if data is None:
            # YAML parser gave up on the LLM output — treat as no suggestions
            # instead of crashing later with 'NoneType' is not subscriptable.
            return {"code_suggestions": []}
        if isinstance(data, list):
            data = {'code_suggestions': data}
        if not isinstance(data, dict) or "code_suggestions" not in data:
            return {"code_suggestions": []}

        # remove or edit invalid suggestions
        suggestion_list = []
        one_sentence_summary_list = []
        for i, suggestion in enumerate(data['code_suggestions']):
            try:
                needed_keys = ['one_sentence_summary', 'label', 'relevant_file']
                is_valid_keys = True
                for key in needed_keys:
                    if key not in suggestion:
                        is_valid_keys = False
                        get_logger().debug(
                            f"Skipping suggestion {i + 1}, because it does not contain '{key}':\n'{suggestion}")
                        break
                if not is_valid_keys:
                    continue

                if get_settings().get("pr_code_suggestions.focus_only_on_problems", False):
                    CRITICAL_LABEL = 'critical'
                    if CRITICAL_LABEL in suggestion['label'].lower(): # we want the published labels to be less declarative
                        suggestion['label'] = 'possible issue'

                if suggestion['one_sentence_summary'] in one_sentence_summary_list:
                    get_logger().debug(f"Skipping suggestion {i + 1}, because it is a duplicate: {suggestion}")
                    continue

                if 'const' in suggestion['suggestion_content'] and 'instead' in suggestion[
                    'suggestion_content'] and 'let' in suggestion['suggestion_content']:
                    get_logger().debug(
                        f"Skipping suggestion {i + 1}, because it uses 'const instead let': {suggestion}")
                    continue

                if ('existing_code' in suggestion) and ('improved_code' in suggestion):
                    suggestion = self._truncate_if_needed(suggestion)
                    # Reject suggestions that look like LLM body-truncation
                    # hallucinations (replacing function bodies with '...')
                    # before they ever reach the GitLab DiffNote pipeline.
                    suggestion = self.validate_suggestion_does_not_truncate_body(suggestion)
                    one_sentence_summary_list.append(suggestion['one_sentence_summary'])
                    suggestion_list.append(suggestion)
                else:
                    get_logger().info(
                        f"Skipping suggestion {i + 1}, because it does not contain 'existing_code' or 'improved_code': {suggestion}")
            except Exception as e:
                get_logger().error("Error processing suggestion {i + 1}: {suggestion}, error: %s", format_exception_chain(e))
        data['code_suggestions'] = suggestion_list

        return data

    def _suppress_resolved_suggestions(self, code_suggestions):
        """Drop suggestions whose (file, line) is already resolved.

        When /improve is re-run after an Apply or /dismiss, the LLM has
        no memory of prior emissions and may produce the same finding
        again.  Looking at the telemetry store, we know which lines
        have been marked applied or dismissed, and we can suppress
        them here so the MR does not collect duplicate DiffNotes.

        Returns the filtered list.  Telemetry / state are not touched.
        """
        if not code_suggestions:
            return code_suggestions
        try:
            mr_id = (
                getattr(self.git_provider, "id_mr", None)
                or getattr(self.git_provider, "pr_id", None)
                or getattr(getattr(self.git_provider, "pr", None), "iid", None)
                or getattr(getattr(self.git_provider, "pr", None), "id", 0)
            )
            raw_project = (
                getattr(self.git_provider, "id_project", None)
                or getattr(self.git_provider, "project_id", None)
                or 0
            )
            project_id = raw_project
            if not isinstance(raw_project, int):
                try:
                    gl = getattr(self.git_provider, "gl", None)
                    if gl is not None and raw_project:
                        project_id = gl.projects.get(raw_project).id
                except Exception:
                    project_id = 0
            if not mr_id:
                return code_suggestions
            pr_url = (
                getattr(self.git_provider, "pr_url", None)
                or getattr(getattr(self.git_provider, "pr", None), "web_url", None)
            )
            existing = telemetry_events.get_default_store().list_suggestions(
                mr_id=int(mr_id),
                project_id=int(project_id) if project_id else None,
                attach_severity=False,
                pr_url=pr_url,
            )
        except Exception as e:
            get_logger().debug(f"suppress-resolved lookup failed (skip): {e}")
            return code_suggestions

        # Build (file, line) -> resolved suggestion metadata. A small
        # line-window (3 lines) catches ordinary re-anchoring. Dismissed
        # suggestions with the same repository rule key get a wider window,
        # because unrelated applies can move the same function by more lines.
        resolved = {}
        for s in existing:
            state = s.get("state")
            if state not in ("applied", "dismissed"):
                continue
            f = (s.get("file") or "").strip()
            ln = s.get("line")
            if not f or not ln:
                continue
            prior_rule_keys = set(telemetry_events.extract_rule_keys_from_text(str(s.get("rule_keys") or "")))
            resolved.setdefault(f, {}).setdefault(ln, []).append({
                "state": state,
                "rule_keys": prior_rule_keys,
            })

        def _states(entries):
            return {entry["state"] for entry in entries} if entries else None

        def _is_resolved(file, line, rule_keys):
            fmap = resolved.get((file or "").strip())
            if not fmap or not line:
                return None
            # exact line hit
            states = _states(fmap.get(line))
            if states:
                return states
            # ±3 line hit (LLM often re-anchors suggestions after the
            # file is updated; the resolved line is typically within
            # the same function body)
            for delta in range(1, 4):
                for candidate in (line - delta, line + delta):
                    states = _states(fmap.get(candidate))
                    if states:
                        return states
            if rule_keys:
                rule_line_window = int(
                    get_settings().pr_code_suggestions.get("resolved_suggestions_rule_line_window", 10)
                )
                for delta in range(4, rule_line_window + 1):
                    for candidate in (line - delta, line + delta):
                        entries = fmap.get(candidate) or []
                        if any(
                            entry["state"] == "dismissed" and rule_keys.intersection(entry["rule_keys"])
                            for entry in entries
                        ):
                            return {"dismissed"}
            return None

        kept = []
        dropped = 0
        for cs in code_suggestions:
            f = cs.get("relevant_file")
            ln = cs.get("relevant_lines_start")
            original_suggestion = cs.get("original_suggestion") or {}
            rule_text = " ".join([
                str(cs.get("body") or ""),
                str(original_suggestion.get("suggestion_content") or ""),
                str(original_suggestion.get("one_sentence_summary") or ""),
            ])
            rule_keys = set(telemetry_events.extract_rule_keys_from_text(rule_text))
            states = _is_resolved(f, ln, rule_keys)
            if states:
                get_logger().info(
                    f"suppress-resolved: skip {f}:{ln} (prior state={sorted(states)})"
                )
                dropped += 1
                continue
            kept.append(cs)
        if dropped:
            get_logger().info(
                f"push_inline_code_suggestions: suppressed {dropped} already-resolved suggestion(s)"
            )
        return kept

    def _dedup_same_round_suggestions(self, code_suggestions):
        """Drop overlapping suggestions emitted in the same LLM round.

        When the LLM produces two patches targeting the same file / start
        line / label (e.g. one inline patch + one full-function replacement
        for the same NO-LOG-EXC fix), only the highest-scoring one is kept.
        Suggestions with different rule labels at the same line are
        preserved, so a DOCSTRING + TYPEHINTS pair on the same function
        is not collapsed.
        """
        groups = {}
        order = []
        for cs in code_suggestions:
            key = (
                (cs.get("relevant_file") or "").strip(),
                cs.get("relevant_lines_start"),
                (cs.get("label") or "").strip(),
            )
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(cs)

        kept = []
        dropped = 0
        for key in order:
            bucket = groups[key]
            if len(bucket) == 1:
                kept.append(bucket[0])
                continue
            bucket.sort(
                key=lambda x: (x.get("original_suggestion") or {}).get("score") or 0,
                reverse=True,
            )
            winner = bucket[0]
            kept.append(winner)
            for loser in bucket[1:]:
                dropped += 1
                get_logger().info(
                    "same-round dedup: drop "
                    f"{loser.get('relevant_file')}:{loser.get('relevant_lines_start')} "
                    f"(label={key[2]!r}, score="
                    f"{(loser.get('original_suggestion') or {}).get('score')}) "
                    f"in favor of higher-scored duplicate (score="
                    f"{(winner.get('original_suggestion') or {}).get('score')})"
                )
        if dropped:
            get_logger().info(
                f"push_inline_code_suggestions: same-round dedup dropped {dropped} duplicate(s)"
            )
        return kept

    async def push_inline_code_suggestions(self, data):
        code_suggestions = []

        if not data['code_suggestions']:
            get_logger().info('No suggestions found to improve this PR.')
            if self.progress_response:
                return self.git_provider.edit_comment(self.progress_response,
                                                      body='No suggestions found to improve this PR.')
            else:
                return self.git_provider.publish_comment('No suggestions found to improve this PR.')

        for d in data['code_suggestions']:
            try:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"suggestion: {d}")
                relevant_file = d['relevant_file'].strip()
                relevant_lines_start = int(d['relevant_lines_start'])  # absolute position
                relevant_lines_end = int(d['relevant_lines_end'])
                content = d['suggestion_content'].rstrip()
                new_code_snippet = d['improved_code'].rstrip()
                # Strip any nested ```suggestion / ``` fenced blocks the LLM may have
                # inlined into the new code (e.g. writing ```suggestion:-N+M at the top).
                # Otherwise the GitLab DiffNote ends up with nested fences and the
                # Apply suggestion button either renders wrong or commits garbage.
                new_code_snippet = re.sub(r"```(?:suggestion[^\n]*|diff)?\n", "", new_code_snippet).rstrip()
                new_code_snippet = re.sub(r"\n```\s*$", "", new_code_snippet).rstrip()
                label = d['label'].strip()

                if new_code_snippet:
                    new_code_snippet = self.dedent_code(relevant_file, relevant_lines_start, new_code_snippet)

                if d.get('score'):
                    body = f"""**Suggestion:** {content} [{label}, importance: {d.get('score')}]\n```suggestion\n{new_code_snippet}\n```\n\n✅ 接受建议\n   • 直接用：点上方「应用建议」按钮\n   • 自己改：回复 `/adopt` [理由]\n\n❌ 关闭建议\n   • 回复 `/dismiss` [理由]\n\n理由会被记录，用于改进后续建议。"""
                else:
                    body = f"""**Suggestion:** {content} [{label}]\n```suggestion\n{new_code_snippet}\n```\n\n✅ 接受建议\n   • 直接用：点上方「应用建议」按钮\n   • 自己改：回复 `/adopt` [理由]\n\n❌ 关闭建议\n   • 回复 `/dismiss` [理由]\n\n理由会被记录，用于改进后续建议。"""
                code_suggestions.append({'body': body, 'relevant_file': relevant_file,
                                         'relevant_lines_start': relevant_lines_start,
                                         'relevant_lines_end': relevant_lines_end,
                                         'label': label,
                                         'original_suggestion': d})
            except Exception:
                get_logger().info(f"Could not parse suggestion: {d}")

        pre_dedup_count = len(code_suggestions)
        code_suggestions = self._dedup_same_round_suggestions(code_suggestions)
        dedup_dropped = pre_dedup_count - len(code_suggestions)
        pre_suppress_count = len(code_suggestions)
        # Capture (file, line, label) for every entry we are about to feed
        # into _suppress_resolved_suggestions so we can tell the reviewer
        # exactly which lines were dropped on the way out.
        suppressed_lines = [
            (cs.get('relevant_file'), cs.get('relevant_lines_start'), (cs.get('label') or '').strip())
            for cs in code_suggestions
        ]
        code_suggestions = self._suppress_resolved_suggestions(code_suggestions)
        suppressed_count = pre_suppress_count - len(code_suggestions)
        self._last_suggestion_outcome = {
            'llm_emitted': pre_dedup_count,
            'dedup_dropped': dedup_dropped,
            'suppressed_count': suppressed_count,
            'kept': len(code_suggestions),
            'suppressed_lines': suppressed_lines[:suppressed_count],
        }
        if not code_suggestions:
            get_logger().info('All LLM suggestions were already applied or dismissed.')
            return
        is_successful = self.git_provider.publish_code_suggestions(code_suggestions)
        # Emit telemetry events for the suggestions we just attempted to publish.
        try:
            mr_id = (
                getattr(self.git_provider, "id_mr", None)
                or getattr(self.git_provider, "pr_id", None)
                or getattr(getattr(self.git_provider, "pr", None), "iid", None)
                or getattr(getattr(self.git_provider, "pr", None), "id", 0)
            )
            raw_project = (
                getattr(self.git_provider, "id_project", None)
                or getattr(self.git_provider, "project_id", None)
                or 0
            )
            # GitLab stores id_project as a namespace path string
            # (e.g. "root/auto-review-test"); resolve to int via the API
            # when needed.
            project_id = raw_project
            if not isinstance(raw_project, int):
                try:
                    gl = getattr(self.git_provider, "gl", None)
                    if gl is not None and raw_project:
                        project_id = gl.projects.get(raw_project).id
                except Exception:
                    project_id = 0
            for cs in code_suggestions:
                original = cs.get("original_suggestion") or {}
                rule_keys = telemetry_events.extract_rule_keys_from_text(
                    original.get("suggestion_content", "") + " " + original.get("one_sentence_summary", "")
                )
                telemetry_events.emit_suggestion(
                    mr_id=mr_id,
                    project_id=project_id,
                    file=cs.get("relevant_file", ""),
                    line=int(cs.get("relevant_lines_start") or 0) or None,
                    label=original.get("label", ""),
                    importance=int(original.get("score") or 0),
                    one_sentence_summary=original.get("one_sentence_summary", ""),
                    rule_keys=rule_keys,
                    score=original.get("score"),
                    note_id=cs.get("note_id"),
                )
        except Exception as e:
            get_logger().warning("telemetry emit on publish_code_suggestions failed: %s", format_exception_chain(e))
        if not is_successful:
            get_logger().info("Failed to publish code suggestions, trying to publish each suggestion separately")
            for code_suggestion in code_suggestions:
                self.git_provider.publish_code_suggestions([code_suggestion])

    def dedent_code(self, relevant_file, relevant_lines_start, new_code_snippet):
        try:  # dedent code snippet
            self.diff_files = self.git_provider.diff_files if self.git_provider.diff_files \
                else self.git_provider.get_diff_files()
            original_initial_line = None
            for file in self.diff_files:
                if file.filename.strip() == relevant_file:
                    if file.head_file:
                        file_lines = file.head_file.splitlines()
                        if relevant_lines_start > len(file_lines):
                            get_logger().warning(
                                "Could not dedent code snippet, because relevant_lines_start is out of range",
                                artifact={'filename': file.filename,
                                          'file_content': file.head_file,
                                          'relevant_lines_start': relevant_lines_start,
                                          'new_code_snippet': new_code_snippet})
                            return new_code_snippet
                        else:
                            original_initial_line = file_lines[relevant_lines_start - 1]
                            if not original_initial_line.strip():
                                for offset in range(1, len(file_lines)):
                                    candidates = (
                                        relevant_lines_start - 1 + offset,
                                        relevant_lines_start - 1 - offset,
                                    )
                                    original_initial_line = next(
                                        (file_lines[index] for index in candidates
                                         if 0 <= index < len(file_lines) and file_lines[index].strip()),
                                        original_initial_line,
                                    )
                                    if original_initial_line.strip():
                                        break
                    else:
                        get_logger().warning("Could not dedent code snippet, because head_file is missing",
                                             artifact={'filename': file.filename,
                                                       'relevant_lines_start': relevant_lines_start,
                                                       'new_code_snippet': new_code_snippet})
                        return new_code_snippet
                    break
            if original_initial_line:
                suggested_initial_line = new_code_snippet.splitlines()[0]
                original_initial_spaces = len(original_initial_line) - len(original_initial_line.lstrip()) # lstrip works both for spaces and tabs
                suggested_initial_spaces = len(suggested_initial_line) - len(suggested_initial_line.lstrip())
                delta_spaces = original_initial_spaces - suggested_initial_spaces
                if delta_spaces != 0:
                    # Detect indentation character from original line
                    indent_char = '\t' if original_initial_line.startswith('\t') else ' '
                    if delta_spaces > 0:
                        new_code_snippet = textwrap.indent(new_code_snippet, delta_spaces * indent_char)
                    else:
                        extra_spaces = -delta_spaces
                        adjusted_lines = []
                        for line in new_code_snippet.splitlines():
                            removable = min(
                                extra_spaces,
                                len(line) - len(line.lstrip(" \t")),
                            )
                            adjusted_lines.append(line[removable:])
                        new_code_snippet = "\n".join(adjusted_lines)
                    new_code_snippet = new_code_snippet.rstrip('\n')
        except Exception as e:
            get_logger().error("Error when dedenting code snippet for file {relevant_file}, error: %s", format_exception_chain(e))

        return new_code_snippet

    def validate_one_liner_suggestion_not_repeating_code(self, suggestion):
        try:
            existing_code = suggestion.get('existing_code', '').strip()
            if '...' in existing_code:
                return suggestion
            new_code = suggestion.get('improved_code', '').strip()

            relevant_file = suggestion.get('relevant_file', '').strip()
            diff_files = self.git_provider.get_diff_files()
            for file in diff_files:
                if file.filename.strip() == relevant_file:
                    # protections
                    if not file.head_file:
                        get_logger().info(f"head_file is empty")
                        return suggestion
                    head_file = file.head_file
                    base_file = file.base_file
                    if existing_code in base_file and existing_code not in head_file and new_code in head_file:
                        suggestion["score"] = 0
                        get_logger().warning(
                            f"existing_code is in the base file but not in the head file, setting score to 0",
                            artifact={"suggestion": suggestion})
        except Exception as e:
            get_logger().exception(f"Error validating one-liner suggestion", artifact={"error": e})

        return suggestion

    def validate_suggestion_does_not_truncate_body(self, suggestion):
        """Guard against LLM body-truncation hallucination.

        When the model is asked to add a docstring / refactor a function, it
        sometimes emits an ``improved_code`` where the original body has been
        replaced with a single ``...`` (Ellipsis) on its own line, leaving the
        function effectively unimplemented. Applying that suggestion via the
        GitLab UI then silently deletes real code (e.g. note 2060 on MR 78
        destroyed four function bodies in ``services/payment_router.py``).

        Heuristic rejection: when the original code has >= 4 non-blank lines,
        the improved code has >= 1 standalone ``...`` line, and the improved
        code lost >= 40% of the line count, set ``score`` to 0 so the
        downstream ``score_threshold`` filter drops it.
        """
        try:
            existing_code = (suggestion.get("existing_code") or "").strip()
            improved_code = (suggestion.get("improved_code") or "").strip()
            if not existing_code or not improved_code:
                return suggestion

            orig_lines = [ln for ln in existing_code.splitlines() if ln.strip()]
            new_lines = [ln for ln in improved_code.splitlines() if ln.strip()]
            ellipsis_count = sum(1 for ln in new_lines if ln.strip() == "...")

            # Count how many *real* (non-Ellipsis) lines disappeared between
            # existing_code and improved_code. If the LLM dropped >= 2 real
            # lines AND introduced at least one Ellipsis placeholder, the
            # suggestion is almost certainly a body-truncation hallucination.
            lost_real_lines = len(orig_lines) - (len(new_lines) - ellipsis_count)

            if (len(orig_lines) >= 4
                    and ellipsis_count >= 1
                    and lost_real_lines >= 2):
                suggestion["score"] = 0
                suggestion["score_why"] = (
                    f"body-truncation guard: improved_code replaces "
                    f"{len(orig_lines)} original lines with "
                    f"{len(new_lines)} lines including {ellipsis_count} "
                    f"standalone '...' Ellipsis — looks like LLM "
                    f"body-truncation hallucination, rejected."
                )
                get_logger().warning(
                    f"validate_suggestion_does_not_truncate_body: rejected "
                    f"{suggestion.get('relevant_file')}:"
                    f"{suggestion.get('relevant_lines_start')} "
                    f"({len(orig_lines)}->{len(new_lines)} lines, "
                    f"{ellipsis_count} '...')"
                )
        except Exception as e:
            get_logger().exception(
                "Error in validate_suggestion_does_not_truncate_body",
                artifact={"error": e},
            )
        return suggestion

    def remove_line_numbers(self, patches_diff_list: List[str]) -> List[str]:
        # create a copy of the patches_diff_list, without line numbers for '__new hunk__' sections
        try:
            self.patches_diff_list_no_line_numbers = []
            for patches_diff in self.patches_diff_list:
                patches_diff_lines = patches_diff.splitlines()
                for i, line in enumerate(patches_diff_lines):
                    if line.strip():
                        if line.isnumeric():
                            patches_diff_lines[i] = ''
                        elif line[0].isdigit():
                            # find the first letter in the line that starts with a valid letter
                            for j, char in enumerate(line):
                                if not char.isdigit():
                                    patches_diff_lines[i] = line[j + 1:]
                                    break
                self.patches_diff_list_no_line_numbers.append('\n'.join(patches_diff_lines))
            return self.patches_diff_list_no_line_numbers
        except Exception as e:
            get_logger().error("Error removing line numbers from patches_diff_list, error: %s", format_exception_chain(e))
            return patches_diff_list

    async def prepare_prediction_main(self, model: str) -> dict:
        # get PR diff
        if get_settings().pr_code_suggestions.decouple_hunks:
            self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                        self.token_handler,
                                                        model,
                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                        add_line_numbers=True)  # decouple hunk with line numbers
            self.patches_diff_list_no_line_numbers = self.remove_line_numbers(self.patches_diff_list)  # decouple hunk

        else:
            # non-decoupled hunks
            self.patches_diff_list_no_line_numbers = get_pr_multi_diffs(self.git_provider,
                                                                        self.token_handler,
                                                                        model,
                                                                        max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                                        add_line_numbers=False)
            self.patches_diff_list = await self.convert_to_decoupled_with_line_numbers(
                self.patches_diff_list_no_line_numbers, model)
            if not self.patches_diff_list:
                # fallback to decoupled hunks
                self.patches_diff_list = get_pr_multi_diffs(self.git_provider,
                                                            self.token_handler,
                                                            model,
                                                            max_calls=get_settings().pr_code_suggestions.max_number_of_calls,
                                                            add_line_numbers=True)  # decouple hunk with line numbers

        if self.patches_diff_list:
            get_logger().info(f"Number of PR chunk calls: {len(self.patches_diff_list)}")
            get_logger().debug(f"PR diff:", artifact=self.patches_diff_list)

            # parallelize calls to AI:
            if get_settings().pr_code_suggestions.parallel_calls:
                prediction_list = await asyncio.gather(
                    *[self._get_prediction(model, patches_diff, patches_diff_no_line_numbers) for
                      patches_diff, patches_diff_no_line_numbers in
                      zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers)])
                self.prediction_list = prediction_list
            else:
                prediction_list = []
                for patches_diff, patches_diff_no_line_numbers in zip(self.patches_diff_list, self.patches_diff_list_no_line_numbers):
                    prediction = await self._get_prediction(model, patches_diff, patches_diff_no_line_numbers)
                    prediction_list.append(prediction)

            data = {"code_suggestions": []}
            for j, predictions in enumerate(prediction_list):  # each call adds an element to the list
                if "code_suggestions" in predictions:
                    score_threshold = max(1, int(get_settings().pr_code_suggestions.suggestions_score_threshold))
                    for i, prediction in enumerate(predictions["code_suggestions"]):
                        try:
                            score = int(prediction.get("score", 1))
                            if score >= score_threshold:
                                data["code_suggestions"].append(prediction)
                            else:
                                get_logger().info(
                                    f"Removing suggestions {i} from call {j}, because score is {score}, and score_threshold is {score_threshold}",
                                    artifact=prediction)
                        except Exception as e:
                            get_logger().error("Error getting PR diff for suggestion {i} in call {j}, error: %s", format_exception_chain(e),
                                               artifact={"prediction": prediction})
            self.data = data
        else:
            get_logger().warning(f"Empty PR diff list")
            self.data = data = None
        return data

    async def convert_to_decoupled_with_line_numbers(self, patches_diff_list_no_line_numbers, model) -> List[str]:
        with get_logger().contextualize(sub_feature='convert_to_decoupled_with_line_numbers'):
            try:
                patches_diff_list = []
                for patch_prompt in patches_diff_list_no_line_numbers:
                    file_prefix = "## File: "
                    patches = patch_prompt.strip().split(f"\n{file_prefix}")
                    patches_new = copy.deepcopy(patches)
                    for i in range(len(patches_new)):
                        if i == 0:
                            prefix = patches_new[i].split("\n@@")[0].strip()
                        else:
                            prefix = file_prefix + patches_new[i].split("\n@@")[0][1:]
                            prefix = prefix.strip()
                        patches_new[i] = prefix + '\n\n' + decouple_and_convert_to_hunks_with_lines_numbers(patches_new[i],
                                                                                                          file=None).strip()
                        patches_new[i] = patches_new[i].strip()
                    patch_final = "\n\n\n".join(patches_new)
                    if model in MAX_TOKENS:
                        max_tokens_full = MAX_TOKENS[
                            model]  # note - here we take the actual max tokens, without any reductions. we do aim to get the full documentation website in the prompt
                    else:
                        max_tokens_full = get_max_tokens(model)
                    delta_output = 2000
                    token_count = self.token_handler.count_tokens(patch_final)
                    if token_count > max_tokens_full - delta_output:
                        get_logger().warning(
                            f"Token count {token_count} exceeds the limit {max_tokens_full - delta_output}. clipping the tokens")
                        patch_final = clip_tokens(patch_final, max_tokens_full - delta_output)
                    patches_diff_list.append(patch_final)
                return patches_diff_list
            except Exception as e:
                get_logger().exception(f"Error converting to decoupled with line numbers",
                                       artifact={'patches_diff_list_no_line_numbers': patches_diff_list_no_line_numbers})
                return []

    def generate_summarized_suggestions(self, data: Dict) -> str:
        try:
            pr_body = t("pr_code_suggestions.header", "## PR Code Suggestions ✨") + "\n\n"

            if len(data.get('code_suggestions', [])) == 0:
                pr_body += t("pr_code_suggestions.no_suggestions", "No suggestions found to improve this PR.")
                return pr_body

            if get_settings().config.is_auto_command:
                pr_body += t("pr_code_suggestions.intro_auto", "Explore these optional code suggestions:") + "\n\n"

            language_extension_map_org = get_settings().language_extension_map_org
            extension_to_language = {}
            for language, extensions in language_extension_map_org.items():
                for ext in extensions:
                    extension_to_language[ext] = language

            pr_body += "<table>"
            header = f"Suggestion"
            delta = 66
            header += "&nbsp; " * delta
            pr_body += f"""<thead><tr><td><strong>Category</strong></td><td align=left><strong>{header}</strong></td><td align=center><strong>Impact</strong></td></tr>"""
            pr_body += """<tbody>"""
            suggestions_labels = dict()
            # add all suggestions related to each label
            for suggestion in data['code_suggestions']:
                label = suggestion['label'].strip().strip("'").strip('"')
                if label not in suggestions_labels:
                    suggestions_labels[label] = []
                suggestions_labels[label].append(suggestion)

            # sort suggestions_labels by the suggestion with the highest score
            suggestions_labels = dict(
                sorted(suggestions_labels.items(), key=lambda x: max([s['score'] for s in x[1]]), reverse=True))
            # sort the suggestions inside each label group by score
            for label, suggestions in suggestions_labels.items():
                suggestions_labels[label] = sorted(suggestions, key=lambda x: x['score'], reverse=True)

            counter_suggestions = 0
            for label, suggestions in suggestions_labels.items():
                num_suggestions = len(suggestions)
                pr_body += f"""<tr><td rowspan={num_suggestions}>{label.capitalize()}</td>\n"""
                for i, suggestion in enumerate(suggestions):

                    relevant_file = suggestion['relevant_file'].strip()
                    relevant_lines_start = int(suggestion['relevant_lines_start'])
                    relevant_lines_end = int(suggestion['relevant_lines_end'])
                    range_str = ""
                    if relevant_lines_start == relevant_lines_end:
                        range_str = f"[{relevant_lines_start}]"
                    else:
                        range_str = f"[{relevant_lines_start}-{relevant_lines_end}]"

                    try:
                        code_snippet_link = self.git_provider.get_line_link(relevant_file, relevant_lines_start,
                                                                            relevant_lines_end)
                    except:
                        code_snippet_link = ""
                    # add html table for each suggestion

                    suggestion_content = suggestion['suggestion_content'].rstrip()
                    CHAR_LIMIT_PER_LINE = 84
                    suggestion_content = insert_br_after_x_chars(suggestion_content, CHAR_LIMIT_PER_LINE)
                    # pr_body += f"<tr><td><details><summary>{suggestion_content}</summary>"
                    existing_code = suggestion['existing_code'].rstrip() + "\n"
                    improved_code = suggestion['improved_code'].rstrip() + "\n"

                    diff = difflib.unified_diff(existing_code.split('\n'),
                                                improved_code.split('\n'), n=999)
                    patch_orig = "\n".join(diff)
                    patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')

                    example_code = ""
                    example_code += f"```diff\n{patch.rstrip()}\n```\n"
                    if i == 0:
                        pr_body += f"""<td>\n\n"""
                    else:
                        pr_body += f"""<tr><td>\n\n"""
                    suggestion_summary = suggestion['one_sentence_summary'].strip().rstrip('.')
                    if "'<" in suggestion_summary and ">'" in suggestion_summary:
                        # escape the '<' and '>' characters, otherwise they are interpreted as html tags
                        get_logger().info(f"Escaped suggestion summary: {suggestion_summary}")
                        suggestion_summary = suggestion_summary.replace("'<", "`<")
                        suggestion_summary = suggestion_summary.replace(">'", ">`")
                    if '`' in suggestion_summary:
                        suggestion_summary = replace_code_tags(suggestion_summary)

                    pr_body += f"""\n\n<details><summary>{suggestion_summary}</summary>\n\n___\n\n"""
                    pr_body += f"""
**{suggestion_content}**

[{relevant_file} {range_str}]({code_snippet_link})

{example_code.rstrip()}
"""
                    if suggestion.get('score_why'):
                        pr_body += f"<details><summary>{t('suggestion.why_label', 'Suggestion importance[1-10]: {score}', score=suggestion['score'])}</summary>\n\n"
                        pr_body += f"__\n\n{t('suggestion.why_prefix', 'Why')}: {suggestion['score_why']}\n\n"
                        pr_body += f"</details>"

                    pr_body += f"</details>"

                    # # add another column for 'score'
                    score_int = int(suggestion.get('score', 0))
                    score_str = f"{score_int}"
                    if get_settings().pr_code_suggestions.new_score_mechanism:
                        score_str = self.get_score_str(score_int)
                    pr_body += f"</td><td align=center>{score_str}\n\n"

                    pr_body += f"</td></tr>"
                    counter_suggestions += 1

                # pr_body += "</details>"
                # pr_body += """</td></tr>"""
            pr_body += """</tr></tbody></table>"""
            return pr_body
        except Exception as e:
            get_logger().info("Failed to publish summarized code suggestions, error: %s", format_exception_chain(e))
            return ""

    def get_score_str(self, score: int) -> str:
        th_high = get_settings().pr_code_suggestions.get('new_score_mechanism_th_high', 9)
        th_medium = get_settings().pr_code_suggestions.get('new_score_mechanism_th_medium', 7)
        if score >= th_high:
            return "High"
        elif score >= th_medium:
            return "Medium"
        else:  # score < 7
            return "Low"

    async def self_reflect_on_suggestions(self,
                                          suggestion_list: List,
                                          patches_diff: str,
                                          model: str,
                                          prev_suggestions_str: str = "",
                                          dedicated_prompt: str = "") -> str:
        if not suggestion_list:
            return ""

        try:
            suggestion_str = ""
            for i, suggestion in enumerate(suggestion_list):
                suggestion_str += f"suggestion {i + 1}: " + str(suggestion) + '\n\n'

            variables = {'suggestion_list': suggestion_list,
                         'suggestion_str': suggestion_str,
                         "diff": patches_diff,
                         'num_code_suggestions': len(suggestion_list),
                         'prev_suggestions_str': prev_suggestions_str,
                         "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
                         'duplicate_prompt_examples': get_settings().config.get('duplicate_prompt_examples', False),
                         'agents_md_rules': self.vars.get('agents_md_rules') or []}
            environment = Environment(undefined=StrictUndefined)

            if dedicated_prompt:
                system_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().get(dedicated_prompt).user).render(variables)
            else:
                system_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.system).render(variables)
                user_prompt_reflect = environment.from_string(
                    get_settings().pr_code_suggestions_reflect_prompt.user).render(variables)

            with get_logger().contextualize(command="self_reflect_on_suggestions"):
                # Same hard-timeout wrapper as the main LLM call above.
                import asyncio as _asyncio2
                _ai_timeout_ref = float(get_settings().config.ai_timeout or 120)
                response_reflect, finish_reason_reflect = await _asyncio2.wait_for(
                    self.ai_handler.chat_completion(
                        model=model,
                        system=system_prompt_reflect,
                        temperature=get_settings().config.temperature,
                        user=user_prompt_reflect),
                    timeout=_ai_timeout_ref,
                )
        except Exception as e:
            get_logger().info("Could not reflect on suggestions, error: %s", format_exception_chain(e))
            return ""
        return response_reflect
