from steam2sqlite import main


def test_main():
    """Runs the script for a brief time"""
    result = main.main(("--limit", "0.1"))
    assert result == 0
