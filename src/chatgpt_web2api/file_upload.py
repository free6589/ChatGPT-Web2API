"""Safe file validation and ChatGPT composer uploads via Chrome CDP."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_FILE_INPUT_SELECTOR = 'input[type="file"]'

_MEDIA_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic",
    ".heif", ".avif", ".ico",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp",
})


def _needs_generic_input(files: list[Path]) -> bool:
    """True when any file is not an image/video (needs the unfiltered input)."""
    return any(p.suffix.lower() not in _MEDIA_EXTENSIONS for p in files)


class UploadError(ValueError):
    """Raised when an upload cannot be safely prepared or attached."""


def max_upload_bytes() -> int:
    raw = os.environ.get("W2A_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        value = int(raw)
    except ValueError as exc:
        raise UploadError("W2A_MAX_UPLOAD_BYTES must be a positive integer") from exc
    if value <= 0:
        raise UploadError("W2A_MAX_UPLOAD_BYTES must be a positive integer")
    return value


def validate_upload_files(paths: Sequence[str | os.PathLike[str]]) -> list[Path]:
    """Resolve and validate local files without restricting their extension."""
    limit = max_upload_bytes()
    resolved: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise UploadError(f"Upload file does not exist: {path}")
        if not path.is_file():
            raise UploadError(f"Upload path is not a regular file: {path}")
        path = path.resolve()
        size = path.stat().st_size
        if size > limit:
            raise UploadError(
                f"Upload file exceeds maximum size ({size} > {limit} bytes): {path.name}"
            )
        resolved.append(path)
    if not resolved:
        raise UploadError("At least one upload file is required")
    return resolved


async def upload_files_to_composer(
    driver, paths: Sequence[str | os.PathLike[str]]
) -> list[Path]:
    """Attach local files to the current ChatGPT composer using CDP."""
    files = validate_upload_files(paths)
    clicked = await driver._js(
        """(function() {
          const labels = ['Attach files', 'Add files', 'Upload files', 'Attach',
            '添加文件等内容', '添加文件', '附加文件', '附加照片', '上传'];
          const buttons = [...document.querySelectorAll('button,[role="button"]')];
          const button = buttons.find(el => {
            const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '')
              .trim().toLowerCase();
            return labels.some(candidate => label === candidate.toLowerCase() ||
              label.includes(candidate.toLowerCase()));
          });
          if (!button) return 'not-found';
          button.click();
          return 'clicked';
        })()"""
    )
    if clicked != "clicked":
        raise UploadError("ChatGPT attachment button was not found")

    document = await driver._cdp("DOM.getDocument", {"depth": -1, "pierce": True})
    document_result = document.get("result", document)
    root_id = document_result.get("root", {}).get("nodeId")
    if not root_id:
        raise UploadError("ChatGPT DOM root was not available after opening attachments")
    # There are several hidden file inputs (photo / video / generic). The
    # generic "attach file" input (no accept filter) must be used for
    # non-media files like .log/.zip — DOM.querySelector returns the first
    # (image-only) input, so enumerate all and pick the best match.
    listed = await driver._cdp(
        "DOM.querySelectorAll", {"nodeId": root_id, "selector": _FILE_INPUT_SELECTOR}
    )
    listed_result = listed.get("result", listed)
    candidate_ids = listed_result.get("nodeIds", [])
    if not candidate_ids:
        raise UploadError("ChatGPT file input was not available after opening attachments")
    node_id = candidate_ids[0]
    if len(candidate_ids) > 1 or _needs_generic_input(files):
        # Fetch accept/multiple attrs for each candidate to pick correctly.
        best = candidate_ids[0]
        best_score = -1
        for cid in candidate_ids:
            attrs_resp = await driver._cdp("DOM.getAttributes", {"nodeId": cid})
            attrs_result = attrs_resp.get("result", attrs_resp)
            flat = attrs_result.get("attributes", []) or []
            attrs = {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
            accept = (attrs.get("accept") or "").lower()
            multiple = "multiple" in attrs
            score = 0
            if _needs_generic_input(files):
                # Generic input has no accept filter (or */*).
                if not accept or "*/*" in accept:
                    score = 10
                    if multiple or len(files) == 1:
                        score += 1
            else:
                if accept:
                    score = 5
                    if multiple or len(files) == 1:
                        score += 1
                else:
                    score = 6
            if score > best_score:
                best_score = score
                best = cid
        node_id = best
    if not node_id:
        raise UploadError("ChatGPT file input was not available after opening attachments")
    await driver._cdp(
        "DOM.setFileInputFiles",
        {"nodeId": node_id, "files": [str(path) for path in files]},
    )
    await driver._js(
        """(function() {
          const input = [...document.querySelectorAll('input[type="file"]')]
            .find(el => el.files && el.files.length);
          if (!input) return 'input-not-found';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.dispatchEvent(new Event('change', {bubbles: true}));
          return 'events-dispatched';
        })()"""
    )
    for _ in range(60):
        state = await driver._js(
            """(function() {
              const text = (document.body && document.body.innerText) || '';
              return /file upload pending/i.test(text) ? 'pending' : 'ready';
            })()"""
        )
        if state == "ready":
            break
        await asyncio.sleep(0.5)
    else:
        raise UploadError("ChatGPT file upload stayed pending for 30 seconds")

    driver._file_upload_active = True
    await driver._js(
        """(function() {
          const visible = el => {
            const style = getComputedStyle(el);
            const box = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' &&
              box.width > 0 && box.height > 0;
          };
          const menuItem = [...document.querySelectorAll('*')].find(el =>
            visible(el) && (el.textContent || '').trim() === 'Upload from computer');
          if (!menuItem) return 'already-closed';
          const button = [...document.querySelectorAll('button,[role="button"]')].find(el =>
            /add files and more/i.test(el.getAttribute('aria-label') || '') && visible(el));
          if (!button) return 'button-not-found';
          button.click();
          return 'closed';
        })()"""
    )
    await driver._cdp(
        "Input.dispatchKeyEvent",
        {"type": "rawKeyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    )
    await driver._cdp(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    )
    return files
