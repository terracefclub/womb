"""Known-message evidence only: absence never establishes a bootstrap stage."""
import re


SETTINGS = ["require_preexisting_user_for_oauth", "disable_stats", "disable_hub",
            "sync_hub_resource_types_daily", "smtp_settings", "job_default_timeout", "base_url", "oauths"]
STAGES = ["input policy", "factory credential rotation", "approved accounts", "approved workspace membership",
          "retained account revocation", "Keycloak provider registration", "input continuity",
          "termination signal handler", "interrupt signal handler", "hangup signal handler"]
MESSAGES = [
    "Windmill provider shape: oauth is not an array.",
    *["Windmill provider display-name kind: " + kind + "." for kind in ["absent", "null", "string", "other"]],
    "Windmill bootstrap stage: protected input validated.",
    "Windmill approved-user bootstrap completed; factory credential rejected.",
    "Windmill launcher stage: waiting for ownership",
    "Windmill launcher stage: ownership acquired",
    "Windmill launcher stage: Bun entry reached.",
    *["Windmill launcher stage: installing " + s for s in STAGES if s.endswith("signal handler")],
    *["Windmill migration stage: " + s for s in ["image load", "original server start", "HTTP readiness", "complete schema ledger"]],
    *["Windmill runtime stage: " + s for s in ["HTTP readiness", "protected bootstrap"]],
    *["Windmill bootstrap failed at " + s + "; ingress remains closed. Raw diagnostics suppressed."
      for s in STAGES + ["instance policy " + key for key in SETTINGS]],
]
CATEGORIES = ["permission denied", "read-only file system", "no such file or directory", "no space left on device",
              "password authentication failed", "connection refused", "invalid argument", "operation not permitted",
              "failed to execute", "exec format error", "illegal instruction", "segmentation fault",
              "unrecognized option", "flock:", "SyntaxError", "TypeError", "ReferenceError", "EACCES", "ENOENT",
              "EINVAL", "EAGAIN", "ENOMEM", "out of memory"]


def summarize_text(text):
    """Emit static strings and explicitly labelled numbers, never raw message suffixes."""
    output = [message for message in MESSAGES if message in text]
    output += ["Observed private log category: " + name for name in CATEGORIES if name.lower() in text.lower()]
    number_labels = ["Windmill provider HTTP status", "Windmill provider oauth count", "Windmill provider Keycloak object count",
                     "Windmill launcher argument count", "Windmill runtime HTTP status",
                     "Windmill resource MemoryPeak", "Windmill resource MemoryCurrent", "Windmill resource TasksCurrent"]
    number_labels += ["Windmill resource " + name + " " + key for name in ["memory.events", "pids.events"]
                      for key in ["low", "high", "max", "oom", "oom_kill", "oom_group_kill", "sock_throttled"]]
    for label in number_labels:
        for value in sorted(set(re.findall(re.escape(label) + r": ([0-9]{1,20})(?![0-9])", text))):
            output.append(label + ": " + value)
    return output


def post_start_completions(raw):
    """A status field alone is not an exit: require a completed systemd code."""
    records = []
    for entry in raw.split("}"):
        code = re.search(r"(?:^|[;{])\s*code=([^;]+);", entry)
        status = re.search(r"(?:^|[;{])\s*status=([0-9]+)(?:[;\s]|$)", entry)
        if not code or not status:
            continue
        kind = code[1].strip()
        if kind in ["exited", "killed", "dumped"]:
            records.append({"kind": kind, "status": int(status[1])})
        else:
            records.append({"kind": "unreported"})
    return records


def summarize_unit(properties):
    """Report exact selected state, without argv, environment, timestamps or paths."""
    allowed = {
        "ActiveState": ["active", "inactive", "activating", "deactivating", "reloading", "failed", "maintenance"],
        "SubState": ["dead", "condition", "start-pre", "start", "start-post", "running", "exited", "stop",
                     "stop-sigterm", "stop-sigkill", "stop-post", "failed", "auto-restart", "auto-restart-queued"],
        "Result": ["success", "resources", "timeout", "exit-code", "signal", "core-dump", "watchdog", "start-limit-hit", "oom-kill", "protocol", "exec-condition"],
    }
    output = []
    for key, values in allowed.items():
        value = properties.get(key)
        if value in values:
            output.append("Windmill unit " + key + ": " + value)
    for key in ["MainPID", "ControlPID"]:
        value = properties.get(key, "")
        if re.fullmatch(r"[0-9]{1,10}", value):
            output.append("Windmill unit " + key + ": " + value)
    for index, entry in enumerate(post_start_completions(properties.get("ExecStartPost", ""))):
        line = "Windmill post-start command " + str(index) + " completion: " + entry["kind"]
        if "status" in entry:
            line += " status " + str(entry["status"])
        output.append(line)
    return output
