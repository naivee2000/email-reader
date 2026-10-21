"""App-local browser dispatch without macOS webbrowser's fork-based os.popen."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from urllib.parse import urlsplit

from email_reader import process_launch


class AppBrowser(webbrowser.BaseBrowser):
    """Open OAuth pages through LaunchServices, never a shell or AppleScript."""

    def open(self, url, new=0, autoraise=True):
        # OAuth libraries receive this handler via their normal webbrowser
        # integration. Never let an unexpected file/application URL launch.
        if not isinstance(url, str) or any(ord(character) < 32 or ord(character) == 127
                                           for character in url):
            raise webbrowser.Error("The sign-in page has an unsupported address")
        try:
            parsed = urlsplit(url)
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise webbrowser.Error("The sign-in page has an unsupported address")
            # Access validates malformed/non-numeric ports as well.
            _ = parsed.port
        except ValueError:
            raise webbrowser.Error("The sign-in page has an unsupported address") from None
        try:
            result = process_launch.run(
                ["/usr/bin/open", url], capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # URLs may contain OAuth state; never log them or tool stderr.
            raise webbrowser.Error("The sign-in page could not be opened") from None
        if result.returncode:
            # Returning False lets webbrowser.open try the next registered
            # macOS controller, which would silently fall back to os.popen.
            raise webbrowser.Error("The sign-in page could not be opened")
        return True


def register_browser_opener() -> None:
    """Prefer the safe handler in this process only; leave OS defaults intact."""
    if sys.platform == "darwin":
        webbrowser.register("email-reader", None, AppBrowser("email-reader"), preferred=True)
