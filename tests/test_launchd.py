"""launchd parsing keeps only the facts Tune-up needs; secrets in the job never come back."""
import plistlib

from storage_utility import launchd

PRINT = """gui/501/ai.hermes.gateway = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/ai.hermes.gateway.plist
\tstate = running

\tenvironment = {
\t\tOPENAI_API_KEY => sk-live-secret
\t\tstate = not-this-one
\t}

\truns = 14
\tpid = 4242
\tlast exit code = 78: EX_CONFIG

\tresource coalition = {
\t\tstate = active
\t}
}
"""


def test_parse_print_reads_only_top_level_status():
    info = launchd.parse_print(PRINT)
    assert info == {"loaded": True, "state": "running", "pid": 4242, "runs": 14, "lastExit": 78}
    assert "secret" not in repr(info)


def test_parse_print_never_exited():
    info = launchd.parse_print("x = {\n\tstate = not running\n\truns = 0\n\tlast exit code = (never exited)\n}\n")
    assert info["state"] == "not running" and info["pid"] == 0 and info["lastExit"] is None


def test_plist_environment_is_reduced_to_hermes_home(tmp_path):
    path = tmp_path / "ai.hermes.gateway.plist"
    data = {"Label": "ai.hermes.gateway", "ProgramArguments": ["/bin/hermes", "gateway", "run"],
            "EnvironmentVariables": {"HERMES_HOME": "/Users/me/.hermes", "ANTHROPIC_API_KEY": "sk-ant-secret", "PATH": "/bin"},
            "KeepAlive": {"SuccessfulExit": False}, "StandardErrorPath": "/Users/me/.hermes/logs/gateway.error.log"}
    path.write_bytes(plistlib.dumps(data))
    job = launchd._job_from_plist(path, launchd._read_plist(path))
    assert job["hermesHome"] == "/Users/me/.hermes" and job["keepAlive"] is True
    assert "secret" not in repr(job) and "PATH" not in repr(job)


def test_service_home_defaults_to_users_dot_hermes():
    assert launchd.service_home({"hermesHome": None, "home": "/Users/me"}) == "/Users/me/.hermes"


def test_label_matching():
    assert launchd.label_matches("ai.hermes.gateway", "hermes*")
    assert launchd.label_matches("ai.hermes.gateway-work", "hermes*")
    assert launchd.label_matches("hermes-dashboard", "hermes*")
    assert not launchd.label_matches("application.com.nousresearch.hermes.1.2", "hermes*")
    assert not launchd.label_matches("com.apple.Finder", "hermes*")


def test_jobs_reads_only_hermes_agents(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "ai.hermes.gateway.plist").write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway", "ProgramArguments": ["/bin/true"]}))
    (agents / "com.other.thing.plist").write_bytes(plistlib.dumps({"Label": "com.other.thing", "ProgramArguments": ["/bin/true"]}))
    (agents / "broken.plist").write_text("not a plist")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert [job["label"] for job in launchd.jobs()] == ["ai.hermes.gateway"]


def test_last_log_prefers_error_lines(tmp_path):
    (tmp_path / "logs").mkdir()
    log = tmp_path / "logs" / "err.log"
    log.write_text("starting\nTraceback\nValueError: bad config\nretrying\n")
    assert launchd.last_log({"stderr": str(log), "hermesHome": str(tmp_path)}) == "ValueError: bad config"
