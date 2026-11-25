# An excerpt of supervisor/api/middleware/security.py (the shape the checker parses).
RE_SLUG = r"[-_.A-Za-z0-9]+"

_V1_PATTERNS = _AppSecurityPatterns(
    api_bypass=re.compile(
        r"^(?:"
        r"|/addons/self/(?!security|update)[^/]+"
        r"|/addons/self/options/config"
        r"|/info"
        r"|/services.*"
        r"|/discovery.*"
        r"|/auth"
        r")$"
    ),
    core_only=re.compile(
        r"^(?:"
        r"/addons/" + RE_SLUG + r"/sys_options"
        r")$"
    ),
    role_access={
        ROLE_DEFAULT: re.compile(
            r"^(?:"
            r"|/.+/info"
            r")$"
        ),
        ROLE_MANAGER: re.compile(r"^(?:|/network/.+)$"),
    },
)
