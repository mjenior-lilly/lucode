from pathlib import Path


def test_setup_is_shell_only_and_does_not_edit_rc_files():
    script = Path("scripts/setup.sh").read_text()
    assert "python3 - <<" not in script
    assert "LUCODE_RC_FILE" not in script
    assert ".bashrc" not in script and ".zshrc" not in script
    assert '"$LUCODE" init' in script
