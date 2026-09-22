from glean.web import attachment_commit, attachment_plan, attachment_release


def test_plan_keeps_the_recorded_main_pdf_path(lib):
    record = lib.load("Miyawaki2024Attention")

    status, body = attachment_plan(lib, record.citekey)

    assert status == 200
    assert body["relative_path"] == (
        "attachments/Miyawaki and Vallis (2024) Attention and convection.pdf"
    )


def test_commit_records_a_new_path_without_a_server_file(lib, index):
    record = lib.load("Miyawaki2024Attention")
    record.attachments = [
        attachment for attachment in record.attachments if attachment.kind != "main"
    ]
    lib.save(record)
    status, planned = attachment_plan(lib, record.citekey)
    assert status == 200
    relative = planned["relative_path"]
    assert not (lib.root / relative).exists()

    status, body = attachment_commit(lib, index, record.citekey, relative)

    assert status == 200
    saved = lib.load(record.citekey)
    main = next(item for item in saved.attachments if item.kind == "main")
    assert main.path == relative


def test_commit_rejects_a_path_the_plan_did_not_choose(lib, index):
    status, body = attachment_commit(
        lib,
        index,
        "Miyawaki2024Attention",
        "attachments/not-the-planned-name.pdf",
    )

    assert status == 400
    assert body["ok"] is False


def test_release_deletes_only_the_server_copy(lib):
    record = lib.load("Miyawaki2024Attention")
    path = lib.root / record.attachments[0].path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n")

    status, body = attachment_release(lib, record.citekey)

    assert status == 200
    assert body["removed"] is True
    assert not path.exists()
    assert lib.load(record.citekey).attachments[0].path == record.attachments[0].path


def test_release_is_quiet_when_the_server_has_no_file(lib):
    status, body = attachment_release(lib, "Miyawaki2024Attention")

    assert status == 200
    assert body["removed"] is False


def test_validated_attachment_relative_rejects_escape_paths(lib):
    assert lib.validated_attachment_relative("attachments/paper.pdf") == (
        "attachments/paper.pdf"
    )
    assert lib.validated_attachment_relative("../secrets.pdf") is None
    assert lib.validated_attachment_relative("attachments/nested/paper.pdf") is None
