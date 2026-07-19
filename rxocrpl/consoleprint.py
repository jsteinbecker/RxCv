RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
CYAN, GREEN, YELLOW, RED, MAGENTA, BLUE = (
      "\033[36m",
      "\033[32m",
      "\033[33m",
      "\033[31m",
      "\033[35m",
      "\033[34m",
)


def _hdr(text: str) -> None:
      """Class/setUp banner, e.g. ``[TestSubstanceConversions] ...``."""
      print(f"\n{BOLD}{MAGENTA}[{text}]{RESET}")


def _test(name: str, intent: str = "") -> None:
      """Test-entry line: arrow + method name in cyan, intent dimmed."""
      tail = f"  {DIM}{intent}{RESET}" if intent else ""
      print(f"  {CYAN}\u2192 {name}{RESET}{tail}")


def _val(text: str) -> None:
      """Intermediate value line, indented and dimmed to green."""
      print(f"    {DIM}{GREEN}{text}{RESET}")


def _sub(text: str) -> None:
      """subTest case label, indented and yellow."""
      _val(f"{DIM}{YELLOW}{text}{RESET}")
