"""The unanswered_dialogs fixture (conftest.py) must decline and record a
modal dialog that a test did not stub, instead of letting it block the run.

Every test in this file is AI-generated (Claude Code); helpers and constants
here are covered by this note.
"""

from PySide6.QtWidgets import QFileDialog, QMessageBox


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_unstubbed_question_is_declined_and_recorded(qapp, unanswered_dialogs):
    answer = QMessageBox.question(None, "Quit", "Disconnect 1 viewer and quit?")
    assert answer == QMessageBox.StandardButton.No
    assert unanswered_dialogs == ["QMessageBox.question: Quit: Disconnect 1 viewer and quit?"]
    unanswered_dialogs.clear()  # expected here; anywhere else it fails the test


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_unstubbed_message_box_exec_is_declined_and_recorded(qapp, unanswered_dialogs):
    box = QMessageBox()
    box.setWindowTitle("Approve")
    box.setText("Allow this viewer?")
    assert box.exec() == QMessageBox.StandardButton.No
    assert unanswered_dialogs == ["QMessageBox.exec: Approve: Allow this viewer?"]
    unanswered_dialogs.clear()


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_unstubbed_save_dialog_is_cancelled_and_recorded(qapp, unanswered_dialogs):
    assert QFileDialog.getSaveFileName(None, "Save log") == ("", "")
    assert unanswered_dialogs == ["QFileDialog.getSaveFileName: Save log"]
    unanswered_dialogs.clear()


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_a_test_can_still_stub_the_dialog_itself(qapp, unanswered_dialogs, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    assert QMessageBox.question(None, "Quit", "Sure?") == QMessageBox.StandardButton.Yes
    assert unanswered_dialogs == []
