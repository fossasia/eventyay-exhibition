import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "exhibition" / "templates" / "exhibitors"
WRAPPER = re.compile(r'<div class="col-md-9 col-md-offset-3">(.*?)</div>', re.DOTALL)
CLASS_ATTR = re.compile(r'\bclass="([^"]*)"')
SUBMIT = re.compile(r"<button\b[^>]*\btype=\"submit\"[^>]*>", re.DOTALL)
NAV = re.compile(r"<(?:button|a)\b[^>]*>", re.DOTALL)


def _wrappers(name: str) -> list[str]:
    return WRAPPER.findall((TEMPLATES / name).read_text(encoding="utf-8"))


def test_submit_buttons_in_offset_column_use_btn_save():
    names = (
        "email_compose.html",
        "email_edit.html",
        "devices.html",
        "vouchers.html",
        "settings.html",
    )
    found = 0
    for name in names:
        for section in _wrappers(name):
            for tag in SUBMIT.findall(section):
                found += 1
                classes = CLASS_ATTR.search(tag)
                assert classes, f"{name}: submit button has no class"
                assert "btn-save" in classes.group(1).split(), f"{name}: {classes.group(1)}"
    assert found >= 8


def test_back_and_cancel_in_offset_column_stay_default_size():
    found = 0
    for name in ("devices.html", "vouchers.html", "settings.html"):
        for section in _wrappers(name):
            for tag in NAV.findall(section):
                if 'type="submit"' in tag:
                    continue
                classes = CLASS_ATTR.search(tag)
                assert classes, f"{name}: nav control has no class"
                assert "btn-save" not in classes.group(1).split(), f"{name}: {classes.group(1)}"
                found += 1
    assert found >= 3
