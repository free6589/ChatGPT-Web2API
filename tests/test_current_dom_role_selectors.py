import inspect
from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.completion_detector import CompletionDetector

def test_current_chatgpt_role_selectors_are_supported():
    src = inspect.getsource(CDPDriver) + inspect.getsource(CompletionDetector)
    assert "data-content-search-unit-key$=\\\":assistant\\\"" in src
    assert "data-content-search-unit-key$=\\\":user\\\"" in src
    assert "data-message-author-role" in src


def test_current_chatgpt_answer_container_selectors_are_supported():
    """The clean-answer reader must resolve the current MarkdownRoot /
    data-markdown-text-style container, not only the legacy .markdown."""
    src = inspect.getsource(CompletionDetector)
    assert "MarkdownRoot" in src
    assert "data-markdown-text-style" in src


def test_current_chatgpt_action_row_selectors_are_supported():
    """Completion fallback must match the current action-row hooks
    (.turn-action-controls + feedback aria-labels), not only testids."""
    src = inspect.getsource(CompletionDetector)
    assert "turn-action-controls" in src
    assert "重新生成" in src or "regenerate" in src.lower()
