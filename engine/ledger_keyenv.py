"""ledger_keyenv.py — [PATCH:keymgr] WO-APP1 T5: display-free .env rewrite
helpers for the MANAGE API KEYS window.

The GUI cannot read /etc/paragon/.env (root:<owner group> 0640 by design),
so key changes are applied AS ROOT in ONE pkexec step that runs this file:

    <venv python> ledger_keyenv.py apply <updates-file>

The updates file (staged 0600 in the per-user temp dir, deleted here as
soon as it is read) holds one line per change, wizard quoting:

    LEDGER_GOOGLE_API_KEY="AIza..."      (set / replace)
    LEDGER_ANTHROPIC_API_KEY=           (empty value = clear the line)

Rules, enforced here no matter what the GUI asked for:
  * every line NOT named in the updates is preserved byte-for-byte —
    comments, blanks, quoting, GCP/service-account lines included;
  * the result must keep at least one working provider (the daemon
    refuses to start with none; so does this rewriter);
  * only the two direct-route keys may be changed — any other variable
    in an updates file is refused;
  * no key value is ever logged or printed.

Pure text functions first (unit-tested hard on temp files via the
LEDGER_ENV_PATH-style seam); the privileged file step is at the bottom.
"""
import os
import sys

GOOGLE_KEY = "LEDGER_GOOGLE_API_KEY"
ANTHROPIC_KEY = "LEDGER_ANTHROPIC_API_KEY"
GCP_PROJECT = "GCP_PROJECT_ID"
ALLOWED_KEYS = (GOOGLE_KEY, ANTHROPIC_KEY)


def parse_env_text(text):
    """KEY -> value for KEY=VALUE / KEY="VALUE" lines; blanks and '#'
    comments ignored. Same tolerant rules as the daemon's load_env."""
    values = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def env_providers(text):
    """Which providers an env text configures, by the daemon's own rule:
    Google = AI Studio key OR GCP project; Anthropic = direct key."""
    vals = parse_env_text(text)
    google_key = bool(vals.get(GOOGLE_KEY, "").strip())
    gcp_project = bool(vals.get(GCP_PROJECT, "").strip())
    anthropic = bool(vals.get(ANTHROPIC_KEY, "").strip())
    return {"google": google_key or gcp_project,
            "google_key": google_key,
            "google_vertex": gcp_project,
            "anthropic": anthropic}


def rewrite_env_text(text, updates):
    """Apply {KEY: value} / {KEY: None=clear} to an env text.

    A named KEY's line is replaced in place (wizard quoting, KEY="value")
    or removed (None). Every other line — comments, blanks, unrelated
    variables, their exact quoting — is kept byte-for-byte. A set KEY
    with no existing line is appended at the end. Raises ValueError for
    a KEY outside ALLOWED_KEYS or if the result would leave the
    appliance with no working AI provider."""
    for key, val in updates.items():
        if key not in ALLOWED_KEYS:
            raise ValueError(
                "Refusing to change %r: only the direct-route API keys "
                "(%s) can be managed here." % (key, ", ".join(ALLOWED_KEYS)))
        if val is not None and not str(val).strip():
            raise ValueError(
                "Refusing to set %s to an empty value — use a clear "
                "instead." % key)
        if val is not None and ('"' in val or "\n" in val or "\r" in val):
            raise ValueError(
                "Refusing to set %s: the value contains a quote or line "
                "break, which cannot be a real API key." % key)

    lines = (text or "").splitlines()
    out = []
    seen = set()
    for raw in lines:
        stripped = raw.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val is None:
                continue                      # clear: drop the line
            out.append('%s="%s"' % (key, val))
        else:
            out.append(raw)
    for key, val in updates.items():
        if key not in seen and val is not None:
            out.append('%s="%s"' % (key, val))
    new_text = "\n".join(out)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    after = env_providers(new_text)
    if not (after["google"] or after["anthropic"]):
        raise ValueError(
            "Refusing: this change would leave the appliance with no AI "
            "provider at all, and the Ledger cannot run that way. Add the "
            "other provider's key first, then clear this one.")
    return new_text


def parse_updates_text(text):
    """The updates-file format -> {KEY: value-or-None}. KEY= (empty) means
    clear. Values may be wizard-quoted."""
    updates = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Bad updates line (no '=').")
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        updates[key.strip()] = val if val else None
    if not updates:
        raise ValueError("The updates file named no changes.")
    return updates


def env_group_gid(env_path):
    """The group that must keep read access to the rewritten env file:
    the group the file already has. The initialization wizard sets it to
    the machine owner's primary group, whatever that account is named,
    so preserving it keeps the daemon readable on any box.
    # [PATCH:owner-from-sudo-user]"""
    return os.stat(env_path).st_gid


def apply_updates(env_path, updates):
    """Read-modify-write env_path atomically with the rules above.
    Keeps root:<existing group> 0640 when running as root (the pkexec
    step); plain 0600 otherwise (unit tests on temp files)."""
    with open(env_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    new_text = rewrite_env_text(text, updates)
    tmp_path = env_path + ".keymgr-tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        if os.geteuid() == 0:
            os.chown(tmp_path, 0, env_group_gid(env_path))   # [PATCH:owner-from-sudo-user]
            os.chmod(tmp_path, 0o640)
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def main(argv):
    if len(argv) != 3 or argv[1] != "apply":
        sys.stderr.write("usage: ledger_keyenv.py apply <updates-file>\n")
        return 2
    updates_path = argv[2]
    env_path = os.environ.get("LEDGER_ENV_PATH") or "/etc/paragon/.env"
    try:
        with open(updates_path, "r", encoding="utf-8") as fh:
            updates_text = fh.read()
    finally:
        # the staged key values must not outlive this step, success or not
        try:
            os.remove(updates_path)
        except OSError:
            pass
    try:
        updates = parse_updates_text(updates_text)
        apply_updates(env_path, updates)
    except (ValueError, OSError) as exc:
        sys.stderr.write(str(exc) + "\n")   # rule texts only; never a value
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
