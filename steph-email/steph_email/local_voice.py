"""Optional local speaker output; no shell and no cloud request."""
import shutil
import subprocess


def speak_local(message):
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        raise ValueError("Local speech requires 1–1000 characters")
    executable = shutil.which("espeak-ng")
    if not executable:
        return {"status": "unavailable", "detail": "Install espeak-ng on the computer with speakers"}
    try:
        # Stdin keeps message text out of process arguments and option parsing.
        completed = subprocess.run([executable, "-v", "en-us", "--stdin"], input=message,
                                   text=True, capture_output=True, timeout=60, check=False)
        return {"status": "played" if completed.returncode == 0 else "failed",
                "detail": "Local speech process completed; audible playback is unverified" if completed.returncode == 0 else "Check local audio output"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "failed", "detail": "Local speech process unavailable or timed out"}
