from pr_agent.servers.apply_pipeline_coordinator import (
    ApplyPipelineCoordinator,
    ApplyPipelineJob,
)


def _job(sha, mr_iid=97):
    return ApplyPipelineJob(
        project_id=34,
        mr_iid=mr_iid,
        sha=sha,
        pr_url=f"http://gitlab/root/repo/-/merge_requests/{mr_iid}",
        data={},
        log_context={},
        commands_conf="apply_commands",
    )


def test_enqueue_deduplicates_same_sha():
    coordinator = ApplyPipelineCoordinator()
    first = _job("sha-1")

    assert coordinator.enqueue(first) == "start"
    assert coordinator.enqueue(first) == "duplicate"


def test_pending_job_keeps_only_latest_sha():
    coordinator = ApplyPipelineCoordinator()
    first = _job("sha-1")
    second = _job("sha-2")
    third = _job("sha-3")

    assert coordinator.enqueue(first) == "start"
    assert coordinator.enqueue(second) == "queued"
    assert coordinator.enqueue(third) == "queued"
    assert coordinator.complete(first) == third
    assert coordinator.complete(third) is None


def test_different_mrs_can_start_independently():
    coordinator = ApplyPipelineCoordinator()

    assert coordinator.enqueue(_job("sha-1", 97)) == "start"
    assert coordinator.enqueue(_job("sha-2", 98)) == "start"


def test_completed_mr_accepts_later_sha():
    coordinator = ApplyPipelineCoordinator()
    first = _job("sha-1")

    assert coordinator.enqueue(first) == "start"
    assert coordinator.complete(first) is None
    assert coordinator.enqueue(_job("sha-2")) == "start"
