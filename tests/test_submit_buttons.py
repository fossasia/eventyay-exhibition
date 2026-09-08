import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "exhibition" / "templates" / "exhibitors"
WRAPPER = re.compile(r'<div class="col-md-9 col-md-offset-3">(.*?)</div>', re.DOTALL)
CLASS_ATTR = re.compile(r'\bclass="([^"]*)"')
SUBMIT = re.compile(r"<button\b[^>]*\btype=\"submit\"[^>]*>", re.DOTALL)
NAV = re.compile(r"<(?:button|a)\b[^>]*>", re.DOTALL)

EXPECTED_SUBMITS = {
    "email_compose.html": 2,
    "email_edit.html": 2,
    "devices.html": 1,
    "vouchers.html": 2,
    "settings.html": 5,
}
EXPECTED_NAV = {
    "devices.html": 1,
    "vouchers.html": 1,
    "settings.html": 4,
}


def _wrappers(name: str) -> list[str]:
    return WRAPPER.findall((TEMPLATES / name).read_text(encoding="utf-8"))


def test_submit_buttons_in_offset_column_use_btn_save():
    for name, expected in EXPECTED_SUBMITS.items():
        found = 0
        for section in _wrappers(name):
            for tag in SUBMIT.findall(section):
                found += 1
                classes = CLASS_ATTR.search(tag)
                assert classes, f"{name}: submit button has no class"
                assert "btn-save" in classes.group(1).split(), f"{name}: {classes.group(1)}"
        assert found == expected, f"{name}: expected {expected} submits, found {found}"


def test_nav_controls_in_offset_column_do_not_use_btn_save():
    for name, expected in EXPECTED_NAV.items():
        found = 0
        for section in _wrappers(name):
            for tag in NAV.findall(section):
                if 'type="submit"' in tag:
                    continue
                classes = CLASS_ATTR.search(tag)
                assert classes, f"{name}: nav control has no class"
                assert "btn-save" not in classes.group(1).split(), f"{name}: {classes.group(1)}"
                found += 1
        assert found == expected, f"{name}: expected {expected} nav controls, found {found}"
