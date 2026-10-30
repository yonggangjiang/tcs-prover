"""The related-works store: staged uploads, PDF text, installs, INDEX.md, notices and folder-batch links."""

from contextlib import contextmanager, nullcontext
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from tests import isolated_state  # noqa: F401 - first: keeps private state out of the checkout.
from retrieval import historical_search
from ui import related_works as rw

PDF_SUPPORTED = historical_search.pdf_supported()


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def inode(path):
    info = os.lstat(path)
    return info.st_dev, info.st_ino


def pdf_bytes(*texts, password=None):
    """A PDF with one page per text (an empty text gives a page without text)."""

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for text in texts:
        page = writer.add_blank_page(300, 300)
        if text:
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            page[NameObject("/Contents")] = writer._add_object(content)
    if password:
        writer.encrypt(password)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def pdf_with_forms(invocations):
    """One page whose content draws the same small text form ``invocations`` times."""

    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    form = DecodedStreamObject()
    form.set_data(b"BT /F1 12 Tf 10 10 Td (Lemma) Tj ET")
    form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100),
                                                   NumberObject(100)]),
                 NameObject("/Resources"): DictionaryObject({
                     NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject(
        {NameObject("/Fm0"): writer._add_object(form)})})
    content = DecodedStreamObject()
    content.set_data(b"/Fm0 Do\n" * invocations)
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def pdf_with_distinct_forms(texts, *, page_text="", padding=b""):
    """One page that shows ``page_text`` and draws one flate-compressed form per text, each ending in ``padding``."""

    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))})
    forms = DictionaryObject()
    for index, text in enumerate(texts):
        form = DecodedStreamObject()
        form.set_data(f"BT /F1 12 Tf 10 10 Td ({text}) Tj ET\n".encode("ascii") + padding)
        form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(100),
                                                       NumberObject(100)]),
                     NameObject("/Resources"): DictionaryObject({NameObject("/Font"): fonts})})
        forms[NameObject(f"/Fm{index}")] = writer._add_object(form.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts, NameObject("/XObject"): forms})
    content = DecodedStreamObject()
    shown = f"BT /F1 12 Tf 20 250 Td ({page_text}) Tj ET\n".encode("ascii") if page_text else b""
    content.set_data(shown + b"".join(f"/Fm{index} Do\n".encode("ascii") for index in range(len(texts))))
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def pdf_with_compressed_page(text, padding_lines):
    """One page whose flate-compressed content shows ``text``, then ``padding_lines`` comment lines."""

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET\n".encode("ascii") + b"% padding\n" * padding_lines)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def fake_extraction(pages=3, problem=""):
    """A stand-in for the extraction child: it writes a text copy (owner-only) or reports a problem."""

    def run(content, text):
        if problem:
            return {"problem": problem}
        body = "\n\n".join(f"[Page {number}]\nText of page {number}." for number in range(1, pages + 1))
        descriptor = os.open(text, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
        return {"pages": pages, "chars": len(body)}

    return run


def refuse(entries):
    raise OSError("audit log refused")


class RecordingStream(io.BytesIO):
    def __init__(self, data, *, stop_at=None, fail=False):
        super().__init__(data if stop_at is None else data[:stop_at])
        self.reads, self.fail = [], fail

    def read(self, size=-1):
        self.reads.append(size)
        if self.fail and len(self.reads) > 1:
            raise TimeoutError("timed out")
        return super().read(size)


class StoreCase(unittest.TestCase):
    """A runs folder with a staging area and one job workspace whose manifest lives in its controller folder."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs = Path(temporary.name).resolve() / "runs"
        self.runs.mkdir()
        self.stage = rw.UploadStage(self.runs / rw.STAGING_FOLDER)
        self.workspace, self.manifest_dir = self.job("2026-10-27_10-00-00_problem")

    def job(self, name):
        workspace = self.runs / name
        workspace.mkdir()
        return workspace, self.runs / ".controller" / name

    def upload(self, name, data):
        return self.stage.receive(name, io.BytesIO(data), len(data))

    def tokens(self, *uploads):
        return [upload["token"] for upload in uploads]

    def install(self, *uploads, workspace=None, manifest_dir=None, **options):
        options.setdefault("run_id", "run-1")
        return rw.install(workspace or self.workspace, manifest_dir or self.manifest_dir, self.tokens(*uploads),
                          self.stage, **options)

    def folder(self, workspace=None):
        return (workspace or self.workspace) / rw.FOLDER

    def manifest(self, manifest_dir=None):
        return json.loads(((manifest_dir or self.manifest_dir) / rw.MANIFEST_NAME).read_text(encoding="utf-8"))

    def index(self, workspace=None):
        return (self.folder(workspace) / rw.INDEX_NAME).read_text(encoding="utf-8")

    @contextmanager
    def two_devices(self):
        """Every folder on a device of its own: os.link between two folders fails with EXDEV."""

        link = os.link

        def cross_device(source, target, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            if src_dir_fd is None or src_dir_fd != dst_dir_fd:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            return link(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

        with patch("os.link", cross_device), patch.object(rw._Folder, "device", lambda folder: str(folder.path)):
            yield

    def tree(self, root):
        """Every path under ``root`` with its type and bytes, for before/after comparisons."""

        result = {}
        for path in sorted(Path(root).rglob("*")):
            key = str(path.relative_to(root))
            if path.is_symlink():
                result[key] = ("link", os.readlink(path))
            elif path.is_dir():
                result[key] = ("dir",)
            else:
                result[key] = ("file", path.read_bytes())
        return result


class ConstantsTests(unittest.TestCase):
    def test_names_limits_and_texts_follow_the_specification(self):
        self.assertEqual((rw.FOLDER, rw.INDEX_NAME, rw.MANIFEST_NAME, rw.STAGING_FOLDER),
                         ("related-works", "INDEX.md", "related-works.json", ".uploads"))
        self.assertEqual((rw.MAX_FILE_BYTES, rw.MAX_ITEMS, rw.MAX_NOTE_CHARS, rw.MAX_TITLE_CHARS,
                          rw.MAX_ORIGINAL_NAME_CHARS, rw.MAX_SAVED_NAME_CHARS),
                         (64 * 1024 * 1024, 200, 2_000_000, 200, 255, 120))
        self.assertEqual((rw.STAGED_KEEP_SECONDS, rw.PDF_TIMEOUT_SECONDS, rw.PDF_MAX_PAGES, rw.PDF_MAX_CHARS,
                          rw.NOTICE_NAMES_MAX_CHARS), (24 * 3600, 120, 2000, 5_000_000, 10_000))
        self.assertEqual(rw.RELATED_WORKS_NOTICE,
                         "RELATED WORKS ADDED:\nThe user added {names} to the related-works folder of your working "
                         "directory; related-works/INDEX.md lists every related work. Read what is relevant.")
        self.assertEqual([rw.PROBLEM_UNAVAILABLE, rw.PROBLEM_ENCRYPTED, rw._pages_problem(), rw._chars_problem(),
                          rw.PROBLEM_NO_TEXT, rw._timeout_problem(), rw.PROBLEM_FAILED], [
            "Text extraction is unavailable: it needs pypdf>=6.20.1,<7.",
            "The PDF is encrypted.",
            "The PDF has more than 2,000 pages.",
            "The PDF text is longer than 5,000,000 characters.",
            "The PDF has no text layer (no OCR).",
            "Text extraction took longer than 2 minutes.",
            "Text extraction failed: the PDF could not be read.",
        ])

    def test_error_carries_its_http_status(self):
        self.assertIsInstance(rw.RelatedWorksError("x"), ValueError)
        self.assertEqual(rw.RelatedWorksError("x").status, 400)
        self.assertEqual(rw.RelatedWorksError("x", 413).status, 413)

    def test_size_text(self):
        for size, expected in ((1, "1 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
                               (1048576, "1.0 MB"), (64 * 1048576, "64.0 MB"), (5 * 1048576 // 2, "2.5 MB")):
            with self.subTest(size=size):
                self.assertEqual(rw.size_text(size), expected)


class SafeNameTests(unittest.TestCase):
    def test_table(self):
        # The steps of the specification (§3.2) exactly: "-" left before an extension stays.
        table = [
            ("paper.pdf", "paper.pdf"),
            ("Mixed.Case.PDF", "Mixed.Case.PDF"),
            ("My Paper (final).pdf", "My-Paper-final-.pdf"),
            ("paper (1).pdf", "paper-1-.pdf"),
            ("x-.tex", "x-.tex"),
            ("a  --  b.txt", "a-b.txt"),
            ("dir/sub/file.txt", "file.txt"),
            ("C:\\Users\\me\\notes.md", "notes.md"),
            ("mixed/dir\\last.tex", "last.tex"),
            ("..hidden", "hidden"),
            ("-rf", "rf"),
            ("trailing.", "trailing"),
            ("a..pdf", "a..pdf"),
            ("a\x00b\nc.pdf", "a-b-c.pdf"),
            ("résumé.pdf", "r-sum-.pdf"),
            ("re\u0301sume\u0301.pdf", "r-sum-.pdf"),
            ("論文.pdf", "pdf"),
            ("§.md", "md"),
            (".pdf", "pdf"),
            ("CON .txt", "CON-.txt"),
            ("CON", "file-CON"),
            ("con.txt", "file-con.txt"),
            ("Nul.tar.gz", "file-Nul.tar.gz"),
            ("lpt9.log", "file-lpt9.log"),
            ("COM1", "file-COM1"),
            ("COM10.txt", "COM10.txt"),
            ("CONSOLE.txt", "CONSOLE.txt"),
            ("INDEX.md", "file-INDEX.md"),
            ("index.MD", "file-index.MD"),
            ("index.md.txt", "index.md.txt"),
            ("", "file"),
            ("....", "file"),
            ("///", "file"),
            ("---.---", "file"),
            (None, "file"),
            ("a" * 200 + ".pdf", "a" * 116 + ".pdf"),
            ("a" * 130, "a" * 120),
            ("a" * 150 + "." + "e" * 20, "a" * 120),
            ("a" * 118 + "-x" + ".pdf", "a" * 116 + ".pdf"),
        ]
        for raw, expected in table:
            with self.subTest(raw=raw):
                self.assertEqual(rw.safe_name(raw), expected)
                self.assertLessEqual(len(expected), rw.MAX_SAVED_NAME_CHARS)

    def test_every_saved_name_is_a_valid_single_component(self):
        for raw in ("../../etc/passwd", "..", ".", "a/..", "\\\\server\\share\\x.pdf", "\u202ereverse.pdf",
                    "x" * 300, "名前" * 50 + ".pdf", "CON.", " INDEX.md "):
            with self.subTest(raw=raw):
                name = rw.safe_name(raw)
                self.assertTrue(rw._valid_name(name), name)
                self.assertNotIn(name, ("", ".", ".."))

    def test_numbering_goes_before_the_extension_and_keeps_the_limit(self):
        self.assertEqual(rw._numbered("paper.pdf", 1), "paper.pdf")
        self.assertEqual(rw._numbered("paper.pdf", 2), "paper-2.pdf")
        self.assertEqual(rw._numbered("notes", 3), "notes-3")
        self.assertEqual(rw._numbered("a.tar.gz", 2), "a.tar-2.gz")
        long = rw.safe_name("b" * 200 + ".pdf")
        self.assertEqual(rw._numbered(long, 12), "b" * 113 + "-12.pdf")


class ReceiveTests(StoreCase):
    def test_a_file_is_streamed_in_blocks_with_its_sha256_into_an_owner_only_folder(self):
        data = os.urandom(5 * 1024 * 1024 // 2)
        stream = RecordingStream(data)
        upload = self.stage.receive("Data set (v2).csv", stream, len(data))
        self.assertEqual(set(upload), {"token", "kind", "name", "originalName", "title", "size", "sizeText", "sha256",
                                       "mediaType", "text", "textProblem", "textStatus"})
        self.assertRegex(upload["token"], r"\A[A-Za-z0-9_-]{24}\Z")
        self.assertEqual({key: value for key, value in upload.items() if key != "token"}, {
            "kind": "file", "name": "Data-set-v2-.csv", "originalName": "Data set (v2).csv", "title": "",
            "size": len(data), "sizeText": "2.5 MB", "sha256": hashlib.sha256(data).hexdigest(), "mediaType": "",
            "text": None, "textProblem": "", "textStatus": ""})
        self.assertEqual(stream.reads, [rw.BLOCK_BYTES, rw.BLOCK_BYTES, len(data) - 2 * rw.BLOCK_BYTES])
        folder = self.stage.root / upload["token"]
        self.assertEqual((folder / "content").read_bytes(), data)
        self.assertEqual(sorted(os.listdir(folder)), ["content", "item.json"])
        self.assertEqual([mode(self.stage.root), mode(folder), mode(folder / "content"), mode(folder / "item.json")],
                         [0o700, 0o700, 0o600, 0o600])
        self.assertEqual(json.loads((folder / "item.json").read_text())["sha256"], upload["sha256"])

    def test_refusals_come_before_anything_is_written(self):
        cases = [
            ("", 3, 400, "Give the file a name."),
            ("   ", 3, 400, "Give the file a name."),
            (None, 3, 400, "Give the file a name."),
            ("a" * 256, 3, 400, "The file name is longer than 255 characters."),
            ("empty.txt", 0, 400, "The file is empty."),
            ("big.pdf", 65 * 1048576, 413, "This file is 65.0 MB; the limit is 64 MB per file."),
            ("odd.txt", -1, 400, "The upload must give the size of the file."),
            ("odd.txt", "3", 400, "The upload must give the size of the file."),
        ]
        for name, length, status, message in cases:
            with self.subTest(name=name, length=length):
                stream = RecordingStream(b"abc")
                with self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.receive(name, stream, length)
                self.assertEqual((str(caught.exception), caught.exception.status), (message, status))
                self.assertEqual(stream.reads, [])
                self.assertFalse(self.stage.root.exists())
        self.upload("a" * 255, b"x")

    def test_the_size_limit_is_the_constant(self):
        with patch.object(rw, "MAX_FILE_BYTES", 1048576):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.stage.receive("big.bin", RecordingStream(b"x" * (1048576 + 1)), 1048576 + 1)
            self.assertEqual(caught.exception.status, 413)
            self.assertEqual(str(caught.exception), "This file is 1.0 MB; the limit is 1 MB per file.")
            self.assertFalse(self.stage.root.exists())
            self.assertEqual(self.upload("fits.bin", b"x" * 1048576)["size"], 1048576)

    def test_a_short_or_failing_stream_leaves_no_partial_upload(self):
        for stream in (RecordingStream(b"x" * 3000, stop_at=1000), RecordingStream(b"x" * (3 * 1048576), fail=True)):
            with self.subTest(fail=stream.fail):
                with self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.receive("cut.txt", stream, 3000 if not stream.fail else 3 * 1048576)
                self.assertEqual(str(caught.exception),
                                 "The upload ended before the whole file arrived; add the file again.")
                self.assertEqual(os.listdir(self.stage.root), [])

    def test_a_disk_error_leaves_no_partial_upload(self):
        with patch.object(rw, "_write_all", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.upload("full.txt", b"abc")
        self.assertIn("Could not save the upload", str(caught.exception))
        self.assertEqual(os.listdir(self.stage.root), [])

    def test_a_linked_staging_folder_is_refused(self):
        elsewhere = self.runs.parent / "elsewhere"
        elsewhere.mkdir()
        self.stage.root.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.upload("a.txt", b"abc")
        self.assertIn("Could not save the upload", str(caught.exception))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_linked_runs_folder_still_works(self):
        linked = self.runs.parent / "runs-link"
        linked.symlink_to(self.runs, target_is_directory=True)
        stage = rw.UploadStage(linked / rw.STAGING_FOLDER)
        upload = stage.receive("a.txt", io.BytesIO(b"abc"), 3)
        rw.install(linked / self.workspace.name, linked / ".controller" / self.workspace.name, [upload["token"]],
                   stage, run_id="run")
        self.assertEqual((self.folder() / "a.txt").read_bytes(), b"abc")
        self.assertEqual(os.listdir(self.stage.root), [])

    def test_a_pdf_is_recognized_by_its_marker_in_the_first_1024_bytes(self):
        with patch.object(rw, "_run_extraction", fake_extraction(problem=rw.PROBLEM_NO_TEXT)) as fake:
            early = self.upload("early.bin", b" " * 1019 + b"%PDF-1.7")
            late = self.upload("late.pdf", b" " * 1020 + b"%PDF-1.7")
        self.assertEqual((early["mediaType"], late["mediaType"]), ("application/pdf", ""))
        self.assertEqual(early["textStatus"], "no extracted text: the PDF has no text layer (no OCR)")
        self.assertEqual(late["textStatus"], "")
        self.assertIsNotNone(fake)

    def test_text_status_texts(self):
        for pages, expected in ((1, "text extracted (1 page)"), (12, "text extracted (12 pages)")):
            with self.subTest(pages=pages), patch.object(rw, "_run_extraction", fake_extraction(pages)):
                upload = self.upload("p.pdf", b"%PDF-1.4 x")
                self.assertEqual(upload["textStatus"], expected)
                self.assertEqual(upload["text"]["pages"], pages)
        for problem in (rw.PROBLEM_UNAVAILABLE, rw.PROBLEM_ENCRYPTED, rw._pages_problem(), rw._chars_problem(),
                        rw.PROBLEM_NO_TEXT, rw._timeout_problem(), rw.PROBLEM_FAILED):
            with self.subTest(problem=problem), patch.object(rw, "_run_extraction", fake_extraction(problem=problem)):
                upload = self.upload("p.pdf", b"%PDF-1.4 x")
                self.assertEqual(upload["textProblem"], problem)
                self.assertEqual(upload["textStatus"], "no extracted text: " + problem[0].lower() + problem[1:-1])
        self.assertEqual(rw.text_status({"kind": "file", "mediaType": "application/pdf", "textProblem":
                                         rw.PROBLEM_ENCRYPTED}), "no extracted text: the PDF is encrypted")


class PdfTextTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.scratch = self.runs.parent / "scratch"
        self.scratch.mkdir()

    def extract(self, body):
        source, target = self.scratch / f"in-{time.monotonic_ns()}.pdf", self.scratch / f"out-{time.monotonic_ns()}"
        source.write_bytes(body)
        return rw.extract_pdf_text(source, target), target

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_the_child_process_writes_the_page_text_beside_the_pdf(self):
        body = pdf_bytes("Lemma one holds", "Lemma two holds")
        upload = self.upload("paper.pdf", body)
        text = "[Page 1]\nLemma one holds\n\n[Page 2]\nLemma two holds"
        self.assertEqual(upload["text"], {"pages": 2, "chars": len(text)})
        self.assertEqual((upload["textProblem"], upload["textStatus"]), ("", "text extracted (2 pages)"))
        folder = self.stage.root / upload["token"]
        self.assertEqual((folder / "text").read_text(encoding="utf-8"), text)
        self.assertEqual(mode(folder / "text"), 0o600)
        item = json.loads((folder / "item.json").read_text())
        self.assertEqual((item["textSize"], item["textSha256"]),
                         (len(text.encode()), hashlib.sha256(text.encode()).hexdigest()))
        self.assertEqual((folder / "content").read_bytes(), body)

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_encrypted_and_unreadable_pdfs_give_a_problem_and_keep_the_file(self):
        for name, body, problem in (("locked.pdf", pdf_bytes("Secret", password="pw"), rw.PROBLEM_ENCRYPTED),
                                    ("broken.pdf", b"%PDF-1.4\nthis is not a PDF body\n%%EOF\n", rw.PROBLEM_FAILED)):
            with self.subTest(name=name):
                upload = self.upload(name, body)
                self.assertEqual((upload["text"], upload["textProblem"], upload["mediaType"]),
                                 (None, problem, "application/pdf"))
                folder = self.stage.root / upload["token"]
                self.assertEqual(sorted(os.listdir(folder)), ["content", "item.json"])
                self.assertEqual((folder / "content").read_bytes(), body)
                self.assertEqual(upload["sha256"], hashlib.sha256(body).hexdigest())

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_extraction_refusals(self):
        self.assertEqual(self.extract(pdf_bytes("", ""))[0], {"problem": "The PDF has no text layer (no OCR)."})
        self.assertEqual(self.extract(b"%PDF-1.4\ngarbage")[0], {"problem": rw.PROBLEM_FAILED})
        self.assertEqual(self.extract(pdf_bytes("x", password="pw"))[0], {"problem": rw.PROBLEM_ENCRYPTED})
        with patch.object(rw, "PDF_MAX_PAGES", 2):
            self.assertEqual(self.extract(pdf_bytes("a", "b", "c"))[0], {"problem": "The PDF has more than 2 pages."})
            self.assertEqual(self.extract(pdf_bytes("a", "b"))[0]["pages"], 2)
        exact = "[Page 1]\nabc\n\n[Page 2]\nde"
        with patch.object(rw, "PDF_MAX_CHARS", len(exact) - 1):
            report, target = self.extract(pdf_bytes("abc", "de"))
            self.assertEqual(report, {"problem": f"The PDF text is longer than {len(exact) - 1:,} characters."})
            self.assertFalse(target.exists())
        with patch.object(rw, "PDF_MAX_CHARS", len(exact)):
            report, target = self.extract(pdf_bytes("abc", "de"))
            self.assertEqual((report, target.read_text()), ({"pages": 2, "chars": len(exact)}, exact))
        with patch.object(historical_search, "pdf_supported", return_value=False):
            self.assertEqual(self.extract(pdf_bytes("abc"))[0], {"problem": rw.PROBLEM_UNAVAILABLE})

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_a_page_with_skipped_forms_is_a_failure_not_partial_text(self):
        self.assertEqual(self.extract(pdf_with_forms(150))[0], {"problem": rw.PROBLEM_FAILED})
        self.assertIn("Lemma", self.extract(pdf_with_forms(20))[1].read_text())

    def main(self, *arguments):
        with patch("sys.stdout", io.StringIO()) as output:
            self.assertEqual(rw.extract_main(["--extract-pdf", *map(str, arguments)]), 0)
        return json.loads(output.getvalue())

    def test_the_text_copy_never_replaces_a_file_or_follows_a_link(self):
        source = self.scratch / "in.pdf"
        # A PDF that would be read if the link were followed.
        source.write_bytes(pdf_bytes("abc") if PDF_SUPPORTED else b"%PDF-1.4")
        link = self.scratch / "link.pdf"
        link.symlink_to(source)
        target = self.scratch / "taken"
        target.write_text("keep")
        self.assertEqual(self.main(link, self.scratch / "new"), {"problem": rw.PROBLEM_FAILED})
        self.assertFalse((self.scratch / "new").exists())
        if PDF_SUPPORTED:
            self.assertEqual(self.main(source, self.scratch / "direct"), {"pages": 1, "chars": len("[Page 1]\nabc")})
            self.assertEqual(self.main(source, target), {"problem": rw.PROBLEM_FAILED})
            self.assertEqual(target.read_text(), "keep")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX only")
    def test_a_fifo_given_as_the_pdf_is_refused_without_waiting(self):
        fifo = self.scratch / "fifo.pdf"
        os.mkfifo(fifo)
        result = {}
        thread = threading.Thread(target=lambda: result.update(report=self.main(fifo, self.scratch / "out")),
                                  daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive():
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # Let the blocked reader go before failing.
            thread.join(5)
            self.fail("The extraction waited for a writer on a FIFO.")
        self.assertEqual(result["report"], {"problem": rw.PROBLEM_FAILED if PDF_SUPPORTED else rw.PROBLEM_UNAVAILABLE})

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_the_reader_is_strict(self):
        body = pdf_bytes("Lemma one")
        offset = body.rindex(b"startxref")
        number = int(body[offset:].split()[1])
        # A wrong cross-reference offset, which pypdf's lenient reader would repair.
        damaged = body[:offset] + b"startxref\n" + str(number + 7).encode() + b"\n%%EOF\n"
        self.assertEqual(self.extract(body)[0], {"pages": 1, "chars": len("[Page 1]\nLemma one")})
        self.assertEqual(self.extract(damaged)[0], {"problem": rw.PROBLEM_FAILED})

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_pypdf_runs_with_the_hardened_configuration(self):
        import pypdf

        apply, seen = pypdf.apply_configuration, []

        def recording(**options):
            seen.append(options)
            return apply(**options)

        with patch.object(pypdf, "apply_configuration", recording):
            self.assertEqual(self.extract(pdf_bytes("abc"))[0]["pages"], 1)
        limit, work = 64 * 1024 * 1024, 256 * 1024 * 1024
        self.assertEqual(seen, [{
            "maximum_declared_stream_length": limit, "array_based_stream_maximum_output_length": limit,
            "jbig2_maximum_output_length": limit, "lzw_maximum_output_length": limit,
            "run_length_maximum_output_length": limit, "zlib_maximum_output_length": limit,
            "brotli_maximum_output_length": limit, "zlib_maximum_recovery_input_length": 100000,
            "image_maximum_buffer_size": limit, "stream_decoding_work_maximum_length": work,
            "stream_filters_maximum_length": 4, "page_tree_maximum_entries": 10000, "page_tree_maximum_depth": 50,
            "xform_maximum_invocations_per_extraction": 100, "jbig2dec_binary": None,
            "disable_legacy_handling": True}])

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_a_stream_that_decodes_past_the_byte_limit_fails(self):
        body = pdf_with_compressed_page("Lemma", 2000)  # About 20 KB once decoded, from a file of about 1 KB.
        self.assertEqual(self.extract(body)[0]["pages"], 1)
        with patch.object(rw, "MAX_FILE_BYTES", 8192):
            self.assertLess(len(body), 8192)
            self.assertEqual(self.extract(body)[0], {"problem": rw.PROBLEM_FAILED})

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_a_form_over_a_decoding_limit_fails_rather_than_dropping_its_text(self):
        # pypdf skips a form it cannot decode and only logs it; the page's own text would be returned alone.
        body = pdf_with_distinct_forms(["Formtext"], page_text="Pagetext", padding=b"% padding comment\n" * 1000)
        report, target = self.extract(body)
        self.assertEqual(report["pages"], 1)
        self.assertEqual(target.read_text(), "[Page 1]\nPagetext\nFormtext")
        with patch.object(rw, "PDF_STREAM_WORK_BYTES", 4000):
            self.assertEqual(self.extract(body)[0], {"problem": rw.PROBLEM_FAILED})
        with patch.object(rw, "MAX_FILE_BYTES", 8192):
            self.assertEqual(self.extract(body)[0], {"problem": rw.PROBLEM_FAILED})

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_page_content_over_the_document_budget_fails(self):
        body = pdf_bytes("Alpha", "Beta", "Gamma")  # Three uncompressed streams of about 36 bytes each.
        with patch.object(rw, "PDF_STREAM_WORK_BYTES", 100):
            self.assertEqual(self.extract(body)[0], {"problem": rw.PROBLEM_FAILED})
        with patch.object(rw, "PDF_STREAM_WORK_BYTES", 200):
            self.assertEqual(self.extract(body)[0]["pages"], 3)

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_a_file_over_the_size_limit_is_not_read_in_part(self):
        body = pdf_bytes("Omega")
        with patch.object(rw, "MAX_FILE_BYTES", len(body) + 50):
            self.assertEqual(self.extract(body)[0]["pages"], 1)
            # Cut at the limit, this file would still be a whole PDF.
            self.assertEqual(self.extract(body + b"\n" * 100)[0], {"problem": rw.PROBLEM_FAILED})

    def test_the_child_command_and_its_entry_point(self):
        self.assertEqual(rw._extraction_command("in", "out"),
                         [sys.executable, "-m", "ui.related_works", "--extract-pdf", "in", "out"])
        self.assertEqual(rw.PDF_MAX_MEMORY_BYTES, 2 * 1024 * 1024 * 1024)
        with patch.object(rw, "extract_main", return_value=0) as main:
            self.assertEqual(rw.child_main(["--extract-pdf", "in", "out"]), 0)
        main.assert_called_once_with(["--extract-pdf", "in", "out"], memory_limit=rw.PDF_MAX_MEMORY_BYTES)

    def test_only_a_few_extractions_run_at_once(self):
        running, peak, guard = [0], [0], threading.Lock()

        def child(command, **options):
            with guard:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.2)
            with guard:
                running[0] -= 1
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"problem": rw.PROBLEM_ENCRYPTED}).encode())

        uploads = []
        with patch.object(rw.subprocess, "run", child):
            threads = [threading.Thread(target=lambda n=n: uploads.append(self.upload(f"p{n}.pdf", b"%PDF-1.4 x")))
                       for n in range(2 * rw.PDF_MAX_PARALLEL + 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(uploads), 2 * rw.PDF_MAX_PARALLEL + 1)
        self.assertEqual(peak[0], rw.PDF_MAX_PARALLEL)
        self.assertTrue(all(upload["textProblem"] == rw.PROBLEM_ENCRYPTED for upload in uploads))

    @unittest.skipUnless(rw._peak_memory() is not None, "Peak memory is read with the resource module")
    def test_the_memory_watch_ends_its_process(self):
        script = ("import sys, time; from ui import related_works as rw; rw._watch_memory(int(sys.argv[1]), 0.01); "
                  "time.sleep(30); print('still running')")
        started = time.monotonic()
        completed = subprocess.run([sys.executable, "-c", script, "1"], cwd=rw.ROOT, capture_output=True, timeout=60)
        self.assertEqual((completed.returncode, completed.stdout), (rw.PDF_MEMORY_EXIT, b""))
        self.assertLess(time.monotonic() - started, 20)

    @unittest.skipUnless(rw._peak_memory() is not None, "Peak memory is read with the resource module")
    def test_a_peak_over_the_ceiling_is_refused_even_when_the_watch_missed_it(self):
        source = self.scratch / "in.pdf"
        source.write_bytes(pdf_bytes("abc") if PDF_SUPPORTED else b"%PDF-1.4")
        with patch.object(rw, "_watch_memory") as watch, patch("sys.stdout", io.StringIO()) as output:
            rw.extract_main(["--extract-pdf", str(source), str(self.scratch / "out")], memory_limit=1)
        watch.assert_called_once_with(1)
        self.assertEqual(json.loads(output.getvalue()), {"problem": rw._memory_problem()})

    def test_the_memory_watchs_exit_is_reported_as_the_memory_limit(self):
        self.assertEqual(rw._memory_problem(), "Text extraction needed more than 2 GB of memory.")
        ended = subprocess.CompletedProcess([], rw.PDF_MEMORY_EXIT, stdout=b"")
        killed = subprocess.CompletedProcess([], -9, stdout=b"")
        with patch.object(rw.subprocess, "run", return_value=ended):
            upload = self.upload("big.pdf", b"%PDF-1.4 big")
        self.assertEqual(upload["textProblem"], "Text extraction needed more than 2 GB of memory.")
        self.assertEqual(upload["textStatus"], "no extracted text: text extraction needed more than 2 GB of memory")
        with patch.object(rw.subprocess, "run", return_value=killed):
            self.assertEqual(self.upload("other.pdf", b"%PDF-1.4 other")["textProblem"], rw.PROBLEM_FAILED)

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    @unittest.skipUnless(rw._peak_memory() is not None, "Peak memory is read with the resource module")
    def test_a_pdf_that_needs_more_memory_than_the_ceiling_gets_no_text_and_is_kept(self):
        # Each form decodes to a 16 MiB inline image that pypdf keeps: about 200 MiB for eight, from 130 KB.
        image = b"q BI /W 4096 /H 4096 /BPC 8 /CS /G ID " + b"\0" * (4096 * 4096) + b" EI Q\n"
        heavy = pdf_with_distinct_forms([f"Word{index}" for index in range(8)], padding=image)
        script = ("import sys; from ui import related_works as rw; rw.PDF_MAX_MEMORY_BYTES = int(sys.argv[1]); "
                  "sys.exit(rw.child_main(sys.argv[2:]))")

        def command(content, text):
            return [sys.executable, "-c", script, str(96 * 1024 * 1024), "--extract-pdf", str(content), str(text)]

        with patch.object(rw, "_extraction_command", command), \
                patch.object(rw, "PDF_MAX_MEMORY_BYTES", 96 * 1024 * 1024):
            refused = self.upload("forms.pdf", heavy)
            light = self.upload("plain.pdf", pdf_bytes("Lemma"))
            limit = rw._memory_problem()
        self.assertEqual((refused["text"], refused["textProblem"]), (None, limit))
        folder = self.stage.root / refused["token"]
        self.assertEqual((sorted(os.listdir(folder)), (folder / "content").read_bytes() == heavy),
                         (["content", "item.json"], True))
        self.assertEqual(light["textStatus"], "text extracted (1 page)")

    def test_a_slow_extraction_is_killed_and_the_pdf_kept(self):
        pid_file = self.scratch / "pid"
        command = [sys.executable, "-c",
                   "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
                   str(pid_file)]
        started = time.monotonic()
        with patch.object(rw, "_extraction_command", return_value=command), patch.object(rw, "PDF_TIMEOUT_SECONDS", 1):
            upload = self.upload("slow.pdf", b"%PDF-1.4 slow")
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(upload["textProblem"], "Text extraction took longer than 1 second.")
        self.assertEqual(upload["textStatus"], "no extracted text: text extraction took longer than 1 second")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        folder = self.stage.root / upload["token"]
        self.assertEqual(((folder / "content").read_bytes(), sorted(os.listdir(folder))),
                         (b"%PDF-1.4 slow", ["content", "item.json"]))

    def test_a_child_that_fails_after_writing_leaves_no_text_copy(self):
        def command(content, text):
            return [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('partial'); sys.exit(1)", str(text)]

        with patch.object(rw, "_extraction_command", command):
            upload = self.upload("half.pdf", b"%PDF-1.4 half")
        self.assertEqual((upload["text"], upload["textProblem"]), (None, rw.PROBLEM_FAILED))
        self.assertEqual(sorted(os.listdir(self.stage.root / upload["token"])), ["content", "item.json"])

    def test_a_text_copy_larger_than_the_text_limit_allows_is_refused(self):
        script = ("import sys; open(sys.argv[1], 'w').write('x' * int(sys.argv[2])); "
                  "print('{\"pages\": 1, \"chars\": %s}' % sys.argv[2])")
        for size, accepted in ((40, True), (41, False)):
            with self.subTest(size=size), patch.object(rw, "PDF_MAX_CHARS", 10), \
                    patch.object(rw, "_extraction_command",
                                 lambda content, text: [sys.executable, "-c", script, str(text), str(size)]):
                upload = self.upload("big.pdf", b"%PDF-1.4 big")
                self.assertEqual(upload["text"], {"pages": 1, "chars": size} if accepted else None)
                self.assertEqual(upload["textProblem"], "" if accepted else rw.PROBLEM_FAILED)
                self.assertEqual("text" in os.listdir(self.stage.root / upload["token"]), accepted)

    def test_unusable_child_reports_are_a_failure(self):
        for script in ("print('not json')", "print('{\"pages\": 0, \"chars\": 5}')",
                       "print('{\"problem\": \"two\\\\nlines\"}')", "import sys; sys.exit(4)",
                       "print('{\"pages\": 2, \"chars\": 5}')"):
            with self.subTest(script=script), patch.object(rw, "_extraction_command",
                                                           return_value=[sys.executable, "-c", script]):
                upload = self.upload("odd.pdf", b"%PDF-1.4 odd")
                self.assertEqual((upload["text"], upload["textProblem"]), (None, rw.PROBLEM_FAILED))
                self.assertEqual(sorted(os.listdir(self.stage.root / upload["token"])), ["content", "item.json"])


class NoteTests(StoreCase):
    def test_a_note_is_staged_as_markdown(self):
        titled = self.stage.note("  Lemma 3 / Smith  ", "\n  The key step.\nSecond line.  \n")
        content = "# Lemma 3 / Smith\n\n\n  The key step.\nSecond line.  \n".encode()
        self.assertEqual({key: value for key, value in titled.items() if key != "token"}, {
            "kind": "note", "name": "Lemma-3-Smith.md", "originalName": "", "title": "Lemma 3 / Smith",
            "size": len(content), "sizeText": f"{len(content)} B", "sha256": hashlib.sha256(content).hexdigest(),
            "mediaType": "", "text": None, "textProblem": "", "textStatus": ""})
        self.assertEqual((self.stage.root / titled["token"] / "content").read_bytes(), content)
        plain = self.stage.note(None, "Only text")
        self.assertEqual((plain["name"], plain["title"]), ("note.md", ""))
        self.assertEqual((self.stage.root / plain["token"] / "content").read_bytes(), b"Only text\n")
        self.assertEqual(self.stage.note("Indexing", "x")["name"], "Indexing.md")
        self.assertEqual(self.stage.note("Index", "x")["name"], "file-Index.md")
        self.assertEqual(self.stage.note("\ud800bad", "x\udfff")["title"], "\ufffdbad")

    def test_the_text_is_kept_as_given_less_its_trailing_newlines(self):
        cases = [
            ("    indented code block\n    second line\n", b"    indented code block\n    second line\n"),
            ("\tTabbed\n\n\n", b"\tTabbed\n"),
            ("CRLF line\r\nlast\r\n", b"CRLF line\r\nlast\r\n"),
            ("  trailing spaces stay  ", b"  trailing spaces stay  \n"),
        ]
        for text, content in cases:
            with self.subTest(text=text):
                note = self.stage.note("", text)
                self.assertEqual((self.stage.root / note["token"] / "content").read_bytes(), content)
                self.assertEqual(note["size"], len(content))
        titled = self.stage.note("Code", "    x = 1\n")
        self.assertEqual((self.stage.root / titled["token"] / "content").read_bytes(), b"# Code\n\n    x = 1\n")

    def test_refusals(self):
        title_problem = "Keep the note title to one line of at most 200 characters."
        cases = [
            ("", "", "Enter the note's text."),
            ("Title", "  \n ", "Enter the note's text."),
            ("Title", None, "Enter the note's text."),
            ("Title", "a\0b", "The note cannot contain NUL characters."),
            ("Ti\0tle", "text", "The note cannot contain NUL characters."),
            ("Two\nlines", "text", title_problem),
            ("Two\rlines", "text", title_problem),
            ("Two\u2028lines", "text", title_problem),
            ("t" * 201, "text", title_problem),
            (5, "text", title_problem),
        ]
        for title, text, message in cases:
            with self.subTest(title=title, text=text):
                with self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.note(title, text)
                self.assertEqual(str(caught.exception), message)
        self.assertFalse(self.stage.root.exists())
        self.assertEqual(self.stage.note("t" * 200, "text")["title"], "t" * 200)
        with patch.object(rw, "MAX_NOTE_CHARS", 10):
            for text in ("x" * 11, "x" * 10 + "\n", " " + "x" * 10):
                with self.subTest(text=text), self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.note("", text)
                self.assertEqual(str(caught.exception), "Keep a note at or below 10 characters.")
            self.stage.note("", "x" * 10)
            self.stage.note("", "x" * 9 + "\n")
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.stage.note("", "x" * 2_000_001)
        self.assertEqual(str(caught.exception), "Keep a note at or below 2,000,000 characters.")


class StagedUploadTests(StoreCase):
    def test_peek_and_get_find_each_staged_upload(self):
        first, second = self.upload("a.txt", b"abc"), self.stage.note("T", "text")
        items = self.stage.peek([first["token"], second["token"]])
        self.assertEqual([item.public() for item in items], [first, second])
        self.assertEqual(self.stage.get(first["token"]).path, self.stage.root / first["token"])
        cases = [
            ([first["token"], "A" * 24], rw.EXPIRED),
            (["../../../etc/passwd"], rw.EXPIRED),
            ([first["token"][:-1]], rw.EXPIRED),
            ([first["token"], first["token"]], "Each upload can be added only once."),
            ("not-a-list", "Send the uploads as a list of upload tokens."),
            ([3], "Send the uploads as a list of upload tokens."),
        ]
        for tokens, message in cases:
            with self.subTest(tokens=tokens):
                with self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.peek(tokens)
                self.assertEqual(str(caught.exception), message)
        self.assertEqual(rw.EXPIRED, "This upload has expired; add the file again.")

    def test_a_damaged_or_changed_upload_has_expired(self):
        damaged, changed, linked = self.upload("a.txt", b"abc"), self.upload("b.txt", b"abc"), self.upload("c", b"x")
        (self.stage.root / damaged["token"] / "item.json").write_text("{")
        (self.stage.root / changed["token"] / "content").write_bytes(b"abcd")
        folder = self.stage.root / linked["token"]
        os.rename(folder, self.runs / "moved")
        folder.symlink_to(self.runs / "moved", target_is_directory=True)
        for upload in (damaged, changed, linked):
            with self.subTest(name=upload["name"]), self.assertRaises(rw.RelatedWorksError) as caught:
                self.stage.get(upload["token"])
            self.assertEqual(str(caught.exception), rw.EXPIRED)

    def test_discard(self):
        upload = self.upload("a.txt", b"abc")
        self.assertTrue(self.stage.discard(upload["token"]))
        self.assertFalse(self.stage.discard(upload["token"]))
        self.assertFalse(self.stage.discard("../" + upload["token"]))
        self.assertFalse(self.stage.discard(None))
        self.assertEqual(os.listdir(self.stage.root), [])
        with self.assertRaises(rw.RelatedWorksError):
            self.stage.peek([upload["token"]])

    def test_a_token_never_reaches_outside_its_own_upload(self):
        # Every target below exists, so only the token check stands between it and its removal.
        (self.workspace / "draft.md").write_text("work")
        upload = self.upload("a.txt", b"abc")
        inner = self.stage.root / upload["token"] / "sub"
        inner.mkdir()
        (inner / "kept").write_text("x")
        before = self.tree(self.runs)
        for token in ("../" + self.workspace.name, "..", ".", "", upload["token"] + "/sub",
                      upload["token"] + "/../" + upload["token"], "./" + upload["token"], upload["token"] + "/"):
            with self.subTest(token=token):
                self.assertFalse(self.stage.discard(token))
                self.assertEqual(self.tree(self.runs), before)

    def test_a_planted_upload_record_outside_the_staging_area_is_never_read(self):
        upload = self.upload("a.txt", b"abc")
        item = json.loads((self.stage.root / upload["token"] / "item.json").read_text())
        for token, folder in (("../planted", self.runs / "planted"), ("..", self.runs), (".", self.stage.root)):
            # A complete upload where the token leads, accepted but for the token check.
            folder.mkdir(exist_ok=True)
            (folder / "content").write_bytes(b"abc")
            (folder / "item.json").write_text(json.dumps({**item, "token": token}))
            with self.subTest(token=token):
                for call in (self.stage.get, lambda value: self.stage.peek([value])):
                    with self.assertRaises(rw.RelatedWorksError) as caught:
                        call(token)
                    self.assertEqual(str(caught.exception), rw.EXPIRED)
                with self.assertRaises(rw.RelatedWorksError):
                    rw.install(self.workspace, self.manifest_dir, [token], self.stage, run_id="run")
        self.assertFalse(self.folder().exists())
        self.stage.peek([upload["token"]])

    def test_a_record_without_the_digest_of_its_text_copy_has_expired(self):
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            upload = self.upload("paper.pdf", b"%PDF-1.4 x")
        path = self.stage.root / upload["token"] / "item.json"
        item = json.loads(path.read_text())
        for digest in ("", "0" * 63, "G" * 64, None):
            with self.subTest(digest=digest):
                path.write_text(json.dumps({**item, "textSha256": digest}))
                with self.assertRaises(rw.RelatedWorksError) as caught:
                    self.stage.get(upload["token"])
                self.assertEqual(str(caught.exception), rw.EXPIRED)
        path.write_text(json.dumps(item))
        self.assertEqual(self.stage.get(upload["token"]).item["textSha256"], item["textSha256"])

    @unittest.skipUnless(PDF_SUPPORTED, "Optional pypdf>=6.20.1,<7 not installed")
    def test_a_text_copy_of_another_size_has_expired(self):
        upload = self.upload("paper.pdf", pdf_bytes("Lemma"))
        self.assertIsNotNone(self.stage.get(upload["token"]).item["text"])
        with (self.stage.root / upload["token"] / "text").open("ab") as stream:
            stream.write(b" appended")
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.stage.get(upload["token"])
        self.assertEqual(str(caught.exception), rw.EXPIRED)

    def test_prune_removes_what_is_older_than_a_day(self):
        now = time.time()
        old, fresh = self.upload("old.txt", b"old"), self.upload("new.txt", b"new")
        day = rw.STAGED_KEEP_SECONDS
        os.utime(self.stage.root / old["token"] / "item.json", (now - day - 60, now - day - 60))
        os.utime(self.stage.root / fresh["token"] / "item.json", (now - day + 60, now - day + 60))
        abandoned = self.stage.root / ("B" * 24)
        abandoned.mkdir()
        (abandoned / "content").write_bytes(b"partial")
        os.utime(abandoned, (now - day - 60, now - day - 60))
        writing = self.stage.root / ("C" * 24)
        writing.mkdir()
        for name, age in ((".tmp-old", day + 60), (".tmp-new", 60), ("not-ours", day + 60)):
            (self.stage.root / name).write_text("x")
            os.utime(self.stage.root / name, (now - age, now - age))
        self.assertEqual(self.stage.prune(now), 3)
        self.assertEqual(sorted(os.listdir(self.stage.root)),
                         sorted([fresh["token"], "C" * 24, ".tmp-new", "not-ours"]))
        self.stage.peek([fresh["token"]])
        self.assertEqual(rw.UploadStage(self.runs / "missing").prune(now), 0)


class InstallTests(StoreCase):
    def test_install_links_the_uploads_and_records_them(self):
        paper, note = self.upload("Paper (final).txt", b"proof sketch"), self.stage.note("Idea", "Use induction.")
        paper_inode = inode(self.stage.root / paper["token"] / "content")
        entries = self.install(paper, note, run_id="2026-10-27_10-00-00_problem")
        folder = self.folder()
        self.assertEqual(sorted(os.listdir(folder)), ["INDEX.md", "Idea.md", "Paper-final-.txt"])
        self.assertEqual(inode(folder / "Paper-final-.txt"), paper_inode)
        self.assertEqual(os.lstat(folder / "Paper-final-.txt").st_nlink, 1)
        self.assertEqual((folder / "Idea.md").read_text(), "# Idea\n\nUse induction.\n")
        modes = [mode(folder), mode(folder / "Paper-final-.txt"), mode(folder / "Idea.md"), mode(folder / "INDEX.md"),
                 mode(self.manifest_dir), mode(self.manifest_dir / rw.MANIFEST_NAME)]
        self.assertEqual(modes, [0o700, 0o600, 0o600, 0o600, 0o700, 0o600])
        self.assertEqual(self.manifest(), {"schemaVersion": 1, "items": entries})
        self.assertEqual(list(entries[0]), ["name", "originalName", "kind", "title", "size", "sha256", "mediaType",
                                            "textName", "pages", "chars", "textProblem", "textSize", "textSha256",
                                            "addedAt", "addedIn"])
        self.assertEqual({key: entries[0][key]
                          for key in ("name", "originalName", "kind", "size", "sha256", "addedIn")},
                         {"name": "Paper-final-.txt", "originalName": "Paper (final).txt", "kind": "file",
                          "size": 12, "sha256": hashlib.sha256(b"proof sketch").hexdigest(),
                          "addedIn": "2026-10-27_10-00-00_problem"})
        self.assertRegex(entries[0]["addedAt"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00\Z")
        self.assertEqual((entries[1]["kind"], entries[1]["title"], entries[1]["name"]), ("note", "Idea", "Idea.md"))
        self.assertEqual(os.listdir(self.stage.root), [])
        self.assertFalse((self.workspace / rw.MANIFEST_NAME).exists())
        self.assertEqual(rw.count(self.workspace, self.manifest_dir), 2)

    def test_exact_index_text(self):
        uploads = [self.upload("data.csv", b"1,2,3")]
        with patch.object(rw, "_run_extraction", fake_extraction(3)):
            uploads.append(self.upload("Paper (final).pdf", b"%PDF-1.4 x"))
        with patch.object(rw, "_run_extraction", fake_extraction(1)):
            uploads.append(self.upload("short.pdf", b"%PDF-1.4 y"))
        with patch.object(rw, "_run_extraction", fake_extraction(problem=rw.PROBLEM_NO_TEXT)):
            uploads.append(self.upload("scan.pdf", b"%PDF-1.4 scan"))
        uploads += [self.stage.note("Lemma 3", "Key step."), self.stage.note("", "Loose idea."),
                    self.upload("résumé notes.md", b"# notes\n")]
        self.install(*uploads)
        self.assertEqual(self.index(), (
            "# Related works\n"
            "\n"
            "Files the user supplied for this problem, oldest first.\n"
            "\n"
            "1. data.csv: 5 B.\n"
            "2. Paper-final-.pdf: PDF, 10 B, 3 pages; text in Paper-final-.pdf.txt. Uploaded as \"Paper (final).pdf\".\n"
            "3. short.pdf: PDF, 10 B, 1 pages; text in short.pdf.txt.\n"
            "4. scan.pdf: PDF, 13 B; no extracted text (the PDF has no text layer (no OCR)).\n"
            "5. Lemma-3.md: note \"Lemma 3\", 21 B.\n"
            "6. note.md: note, 12 B.\n"
            "7. r-sum-notes.md: 8 B. Uploaded as \"résumé notes.md\".\n"))
        folder = self.folder()
        self.assertEqual(sorted(os.listdir(folder)), sorted([
            "INDEX.md", "data.csv", "Paper-final-.pdf", "Paper-final-.pdf.txt", "short.pdf", "short.pdf.txt",
            "scan.pdf", "Lemma-3.md", "note.md", "r-sum-notes.md"]))
        self.assertTrue((folder / "Paper-final-.pdf.txt").read_text().startswith("[Page 1]\n"))
        entry = self.manifest()["items"][1]
        self.assertEqual({key: entry[key] for key in ("mediaType", "textName", "pages", "textProblem")},
                         {"mediaType": "application/pdf", "textName": "Paper-final-.pdf.txt", "pages": 3,
                          "textProblem": ""})
        self.assertEqual(rw.index_text(self.manifest()["items"]), self.index())

    def test_a_linked_or_non_folder_related_works_path_is_refused(self):
        upload = self.upload("a.txt", b"abc")
        elsewhere = self.runs.parent / "outside"
        elsewhere.mkdir()
        self.folder().symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.install(upload)
        self.assertEqual(str(caught.exception),
                         "The related-works folder of this run is not a plain folder; related works cannot be added.")
        self.assertEqual(os.listdir(elsewhere), [])
        self.folder().unlink()
        self.folder().write_text("a file")
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.install(upload)
        self.assertEqual(str(caught.exception), rw.NOT_PLAIN)
        self.assertFalse(self.manifest_dir.exists())
        self.stage.peek([upload["token"]])

    def test_names_are_unique_case_insensitively_and_never_overwrite(self):
        first = self.install(self.upload("Paper.PDF", b"one"), self.upload("notes.txt", b"two"))
        self.assertEqual([entry["name"] for entry in first], ["Paper.PDF", "notes.txt"])
        (self.folder() / "planted.txt").write_bytes(b"the author's file")
        (self.folder() / "Planted-2.txt").write_bytes(b"another")
        second = self.install(self.upload("paper.pdf", b"three"), self.upload("NOTES.txt", b"four"),
                              self.upload("notes.TXT", b"five"), self.upload("planted.txt", b"six"),
                              self.upload("INDEX.md", b"seven"), self.upload("index.md", b"eight"))
        self.assertEqual([entry["name"] for entry in second], [
            "paper-2.pdf", "NOTES-2.txt", "notes-3.TXT", "planted-3.txt", "file-INDEX.md", "file-index-2.md"])
        self.assertEqual((self.folder() / "planted.txt").read_bytes(), b"the author's file")
        self.assertEqual((self.folder() / "Planted-2.txt").read_bytes(), b"another")
        self.assertEqual((self.folder() / "Paper.PDF").read_bytes(), b"one")
        self.assertEqual((self.folder() / "planted-3.txt").read_bytes(), b"six")
        self.assertTrue(self.index().startswith("# Related works\n"))
        self.assertEqual(len(self.manifest()["items"]), 8)

    def test_planted_links_are_never_followed_or_written_through(self):
        outside = self.runs.parent / "outside.txt"
        outside.write_text("outside")
        self.folder().mkdir(mode=0o755)
        os.chmod(self.folder(), 0o755)
        (self.folder() / "INDEX.md").symlink_to(outside)
        (self.folder() / "paper.pdf").symlink_to(outside)
        entries = self.install(self.upload("paper.pdf", b"paper"))
        self.assertEqual(entries[0]["name"], "paper-2.pdf")
        self.assertEqual(outside.read_text(), "outside")
        self.assertTrue((self.folder() / "paper.pdf").is_symlink())
        self.assertFalse((self.folder() / "INDEX.md").is_symlink())
        self.assertIn("1. paper-2.pdf: 5 B.", self.index())
        self.assertEqual((mode(self.folder()), mode(self.folder() / "INDEX.md")), (0o700, 0o600))

    def test_a_folder_planted_at_index_md_is_moved_aside(self):
        self.install(self.upload("a.txt", b"abc"))
        index = self.folder() / rw.INDEX_NAME
        index.unlink()
        index.mkdir()
        (index / "x").write_text("the author's")
        self.install(self.upload("b.txt", b"b"))
        self.assertTrue(index.is_file())
        self.assertEqual(self.index().splitlines()[4:], ["1. a.txt: 3 B.", "2. b.txt: 1 B."])
        aside = [name for name in os.listdir(self.folder()) if name.startswith(".INDEX.md.")]
        self.assertEqual(len(aside), 1)
        self.assertRegex(aside[0], r"\A\.INDEX\.md\.[0-9a-f]{8}\.replaced\Z")
        self.assertEqual((self.folder() / aside[0] / "x").read_text(), "the author's")

    def test_an_index_md_that_cannot_be_written_is_refused_in_plain_words(self):
        self.install(self.upload("a.txt", b"abc"))
        tree, raw = self.tree(self.workspace), (self.manifest_dir / rw.MANIFEST_NAME).read_bytes()
        upload = self.upload("b.txt", b"b")
        refusal = IsADirectoryError(errno.EISDIR, "Is a directory", ".tmp-0123456789abcdef", "INDEX.md")
        with patch.object(rw, "_replace_file", side_effect=refusal):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload)
        self.assertEqual(str(caught.exception),
                         "Could not add the related works: INDEX.md could not be written (is a directory)")
        self.assertEqual((self.tree(self.workspace), (self.manifest_dir / rw.MANIFEST_NAME).read_bytes()), (tree, raw))
        self.stage.peek([upload["token"]])

    def test_a_related_works_folder_swapped_after_its_check_is_refused(self):
        upload = self.upload("a.txt", b"abc")
        swapped, elsewhere, open_ = self.workspace / "swapped", self.runs.parent / "elsewhere", os.open
        elsewhere.mkdir()
        for replacement in ("another folder", "a link"):
            with self.subTest(replacement=replacement):
                self.folder().mkdir(mode=0o700)

                def swapping(path, flags, mode=0o777, *, dir_fd=None):
                    if path == rw.FOLDER and dir_fd is not None and not swapped.exists():
                        os.rename(self.folder(), swapped)  # Something else takes the name between check and open.
                        if replacement == "a link":
                            self.folder().symlink_to(elsewhere, target_is_directory=True)
                        else:
                            os.mkdir(self.folder(), 0o700)
                    return open_(path, flags, mode, dir_fd=dir_fd)

                with patch("os.open", swapping):
                    with self.assertRaises(rw.RelatedWorksError) as caught:
                        self.install(upload)
                self.assertEqual(str(caught.exception), rw.NOT_PLAIN)
                self.assertEqual((os.listdir(self.folder()), os.listdir(swapped), os.listdir(elsewhere)), ([], [], []))
                self.stage.peek([upload["token"]])
                if self.folder().is_symlink():
                    self.folder().unlink()
                else:
                    self.folder().rmdir()
                swapped.rmdir()

    def test_a_staged_file_swapped_before_its_link_is_never_installed(self):
        outside = self.runs.parent / "outside.txt"
        outside.write_bytes(b"xyz")
        for swap in ("other bytes", "a link"):
            with self.subTest(swap=swap):
                upload = self.upload("a.txt", b"abc")
                content, link, created = self.stage.root / upload["token"] / "content", os.link, []

                def swapping(source, target, *, dst_dir_fd=None, **options):
                    if source == "content":
                        os.rename(content, content.with_name("original"))  # Something else takes the name.
                        if swap == "a link":
                            content.symlink_to(outside)
                        else:
                            content.write_bytes(b"xyz")
                    link(source, target, dst_dir_fd=dst_dir_fd, **options)
                    info = os.stat(target, dir_fd=dst_dir_fd, follow_symlinks=False)
                    created.append((info.st_dev, info.st_ino))

                with patch("os.link", swapping):
                    with self.assertRaises(rw.RelatedWorksError) as caught:
                        self.install(upload)
                self.assertEqual(str(caught.exception), "Could not add the related works: [Errno 5] The new link a.txt "
                                                        "does not name the saved file")
                self.assertFalse(self.folder().exists())
                self.assertFalse(self.manifest_dir.exists())
                # Not even for a moment does the folder hold a hard link to a file outside.
                self.assertNotIn(inode(outside), created)
                self.assertEqual(outside.read_bytes(), b"xyz")

    def test_a_copy_swapped_before_it_takes_its_name_is_never_installed(self):
        upload = self.upload("a.txt", b"abc")
        self.install(upload, keep_staged=True)  # The upload is now installed, so later installs copy it.
        workspace, manifest_dir = self.job("swap")
        folder, link = self.folder(workspace), os.link

        def swapping(source, target, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            if src_dir_fd != dst_dir_fd:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            if source.startswith(".tmp-"):
                (folder / "planted").write_bytes(b"xyz")
                os.replace(folder / "planted", folder / source)  # The author swaps the copy beside its name.
            return link(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)

        with patch("os.link", swapping), patch.object(rw._Folder, "device", lambda place: str(place.path)):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload, workspace=workspace, manifest_dir=manifest_dir)
        self.assertEqual(str(caught.exception),
                         "Could not add the related works: [Errno 5] The new file a.txt does not name the copy")
        names = os.listdir(folder)
        self.assertNotIn("a.txt", names)
        self.assertEqual([(folder / name).read_bytes() for name in names], [b"xyz"])  # The author's file is kept.
        self.assertFalse((manifest_dir / rw.MANIFEST_NAME).exists())

    def test_an_undone_install_leaves_a_file_that_replaced_its_own(self):
        upload = self.upload("a.txt", b"abc")

        def failing(manifest_dir, entries):
            replacement = self.folder() / "replacement"
            replacement.write_bytes(b"the author's")
            os.replace(replacement, self.folder() / "a.txt")  # The author replaces the new file before the undo.
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        with patch.object(rw, "_write_manifest", failing):
            with self.assertRaises(rw.RelatedWorksError):
                self.install(upload)
        self.assertEqual(sorted(os.listdir(self.folder())), ["a.txt"])
        self.assertEqual((self.folder() / "a.txt").read_bytes(), b"the author's")
        self.stage.peek([upload["token"]])

    def test_controller_folders_are_created_owner_only(self):
        self.install(self.upload("a.txt", b"abc"))
        batch = rw.BatchStore(rw.batch_directory(self.runs / ".controller", "b_0000000002"))
        batch.install(self.tokens(self.upload("b.txt", b"b")), self.stage)
        controller = self.runs / ".controller"
        self.assertEqual([mode(path) for path in (self.stage.root, controller, self.manifest_dir,
                                                  controller / rw.BATCH_FOLDER, batch.root, batch.root / rw.FOLDER)],
                         [0o700] * 6)

    def test_two_installs_of_one_upload_never_share_its_inode(self):
        upload = self.upload("a.txt", b"abc")
        jobs = [self.job(f"race-{number}") for number in range(2)]
        barrier, link, errors = threading.Barrier(2), os.link, []

        def meeting(*args, **options):
            try:
                barrier.wait(0.5)  # Without the staging lock, both installs reach their link here together.
            except threading.BrokenBarrierError:
                pass
            return link(*args, **options)

        def run(workspace, manifest_dir):
            try:
                rw.install(workspace, manifest_dir, [upload["token"]], self.stage, run_id=workspace.name,
                           keep_staged=True)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with patch("os.link", meeting):
            threads = [threading.Thread(target=run, args=job) for job in jobs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        files = [self.folder(workspace) / "a.txt" for workspace, _ in jobs]
        self.assertNotEqual(inode(files[0]), inode(files[1]))
        self.assertEqual(sorted(os.lstat(path).st_nlink for path in files), [1, 2])
        self.assertEqual([path.read_bytes() for path in files], [b"abc", b"abc"])

    def test_index_md_and_copies_are_staged_outside_the_workspace(self):
        create, temporaries = rw._Folder.create, []

        def recording(folder, name):
            if name.startswith(".tmp-"):
                temporaries.append(folder.path)
            return create(folder, name)

        with patch.object(rw._Folder, "create", recording):
            self.install(self.upload("a.txt", b"abc"))
            batch = rw.BatchStore(rw.batch_directory(self.runs / ".controller", "b_0000000001"))
            batch.install(self.tokens(self.upload("b.txt", b"b")), self.stage)
            other, other_manifest = self.job("other")
            batch.link_into(other, other_manifest, run_id="other")
            rw.copy_into(self.workspace, self.manifest_dir, other, other_manifest, run_id="other")
        # INDEX.md and the manifest of each call, and the copy of a.txt (before its INDEX.md and manifest).
        self.assertEqual(temporaries, [self.stage.root, self.manifest_dir, self.stage.root, batch.root,
                                       other_manifest, other_manifest, other_manifest, other_manifest, other_manifest])
        self.assertFalse(any(workspace in path.parents or path == workspace
                             for path in temporaries for workspace in (self.workspace, other)))
        self.assertFalse([name for name in os.listdir(other_manifest) if name.startswith(".tmp-")])

    def test_an_index_staging_area_on_another_device_falls_back_beside_the_index(self):
        rename = os.rename

        def cross_device(source, target, *, src_dir_fd=None, dst_dir_fd=None):
            if src_dir_fd is not None and os.fstat(src_dir_fd) != os.fstat(dst_dir_fd):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            return rename(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with patch("os.rename", cross_device):
            self.install(self.upload("a.txt", b"abc"))
        self.assertEqual(sorted(os.listdir(self.folder())), ["INDEX.md", "a.txt"])
        self.assertIn("1. a.txt: 3 B.", self.index())
        self.assertEqual(os.listdir(self.stage.root), [])

    def test_uniqueness_is_decided_before_the_file_system_is_asked(self):
        # A case-insensitive file system would refuse the names anyway; on others only the check prevents twins.
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            self.install(self.upload("notes.txt", b"one"), self.upload("x.pdf", b"%PDF-1.4 x"))
        place, attempted = rw._place, []

        def recording(source, source_name, size, sha256, folder, name, **options):
            attempted.append(name)
            return place(source, source_name, size, sha256, folder, name, **options)

        with patch.object(rw, "_place", recording):
            entries = self.install(self.upload("NOTES.txt", b"two"), self.upload("Notes.TXT", b"three"),
                                   self.upload("X.PDF.TXT", b"four"))
        self.assertEqual([entry["name"] for entry in entries], ["NOTES-2.txt", "Notes-3.TXT", "X.PDF-2.TXT"])
        self.assertEqual(attempted, ["NOTES-2.txt", "Notes-3.TXT", "X.PDF-2.TXT"])

    def test_a_copy_never_replaces_a_file_at_its_name(self):
        planted = self.folder()
        planted.mkdir()
        (planted / "a.txt").write_bytes(b"the author's")
        for case, links in (("no hard links", patch("os.link", side_effect=OSError(errno.EXDEV, "Cross-device link"))),
                            ("staging on another device", self.two_devices())):
            with self.subTest(case=case), links:
                entries = self.install(self.upload("a.txt", b"upload"))
                self.assertEqual(entries[0]["name"], "a-2.txt" if case == "no hard links" else "a-3.txt")
                self.assertEqual((planted / entries[0]["name"]).read_bytes(), b"upload")
                self.assertEqual((planted / "a.txt").read_bytes(), b"the author's")
                self.assertFalse([name for name in os.listdir(planted) if name.startswith(".tmp-")])

    def test_a_copy_takes_its_name_only_when_complete(self):
        data = b"COPY" * (3 * rw.BLOCK_BYTES // 4)
        upload = self.upload("paper.bin", data)
        self.install(upload, keep_staged=True)  # The upload is now installed, so later installs copy it.
        write, create, seen, created = rw._write_all, rw._Folder.create, [], []

        def recording_write(descriptor, block):
            if bytes(block[:4]) == b"COPY":
                seen.append(sorted(os.listdir(folder)))  # What a crash at this point would leave.
            return write(descriptor, block)

        def recording_create(place, name):
            created.append((place.path, name))
            return create(place, name)

        for devices in ("one device", "two devices"):
            with self.subTest(devices=devices):
                workspace, manifest_dir = self.job(devices.replace(" ", "-"))
                folder = self.folder(workspace)
                seen.clear()
                created.clear()
                with self.two_devices() if devices == "two devices" else nullcontext(), \
                        patch.object(rw, "_write_all", recording_write), \
                        patch.object(rw._Folder, "create", recording_create):
                    entry = self.install(upload, workspace=workspace, manifest_dir=manifest_dir, keep_staged=True)[0]
                self.assertEqual((entry["name"], (folder / "paper.bin").read_bytes()), ("paper.bin", data))
                self.assertEqual(os.lstat(folder / "paper.bin").st_nlink, 1)
                self.assertTrue(seen)
                self.assertFalse(any("paper.bin" in names for names in seen), seen)
                # The copy is staged outside the workspace when it can be linked from there, else beside its name.
                beside = [name for place, name in created if place == folder and name.startswith(".tmp-")]
                self.assertEqual(len(beside), 0 if devices == "one device" else 1)
                self.assertFalse([name for name in os.listdir(folder) if name.startswith(".tmp-")])
                self.assertFalse([name for name in os.listdir(self.stage.root) if name.startswith(".tmp-")])

    def test_a_crash_during_a_copy_leaves_no_partial_file_under_its_name(self):
        data = os.urandom(3 * rw.BLOCK_BYTES)
        upload = self.upload("paper.bin", data)
        child = textwrap.dedent("""
            import errno, os, sys
            from pathlib import Path
            from ui import related_works as rw

            link, written = os.link, []

            def cross_device(source, target, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
                if src_dir_fd != dst_dir_fd:
                    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
                return link(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                            follow_symlinks=follow_symlinks)

            def write(descriptor, data, write=rw._write_all):
                write(descriptor, data)
                written.append(len(data))
                if len(written) == 2:
                    os._exit(9)  # The server dies in the middle of the copy.

            os.link, rw._write_all = cross_device, write
            rw._Folder.device = lambda folder: str(folder.path)  # The staging area is on another device.
            runs, job = Path(sys.argv[1]), sys.argv[2]
            rw.install(runs / job, runs / ".controller" / job, [sys.argv[3]], rw.UploadStage(runs / ".uploads"),
                       run_id=job)
        """)
        completed = subprocess.run([sys.executable, "-c", child, str(self.runs), self.workspace.name, upload["token"]],
                                   cwd=rw.ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self.assertEqual(completed.returncode, 9, completed.stderr.decode(errors="replace"))
        names = os.listdir(self.folder())
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith(".tmp-"))
        self.assertFalse((self.manifest_dir / rw.MANIFEST_NAME).exists())
        entry = self.install(upload)[0]
        self.assertEqual((entry["name"], (self.folder() / "paper.bin").read_bytes()), ("paper.bin", data))

    def test_a_text_copy_name_counts_as_taken(self):
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            self.install(self.upload("x.pdf", b"%PDF-1.4 x"))
        entries = self.install(self.upload("x.pdf.txt", b"plain"), self.upload("x.pdf", b"other"))
        self.assertEqual([entry["name"] for entry in entries], ["x.pdf-2.txt", "x-2.pdf"])
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            pdf = self.upload("y.pdf", b"%PDF-1.4 y")
        (self.folder() / "y.pdf.txt").write_text("planted")
        entry = self.install(pdf)[0]
        self.assertEqual((entry["name"], entry["textName"]), ("y-2.pdf", "y-2.pdf.txt"))
        self.assertFalse((self.folder() / "y.pdf").exists())
        self.assertEqual((self.folder() / "y.pdf.txt").read_text(), "planted")

    def test_numbered_long_names_keep_the_limit(self):
        name = "n" * 140 + ".pdf"
        entries = [self.install(self.upload(name, b"x"))[0] for _ in range(3)]
        self.assertEqual([entry["name"] for entry in entries], ["n" * 116 + ".pdf", "n" * 114 + "-2.pdf",
                                                                "n" * 114 + "-3.pdf"])
        self.assertTrue(all(len(entry["name"]) <= 120 for entry in entries))

    def test_the_item_limit_is_checked_before_anything_is_written(self):
        uploads = [self.upload(f"{n}.txt", b"x") for n in range(3)]
        with patch.object(rw, "MAX_ITEMS", 2):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(*uploads)
            self.assertEqual(str(caught.exception), "A job can hold at most 2 related works.")
            self.assertFalse(self.folder().exists())
            self.assertFalse(self.manifest_dir.exists())
            self.install(*uploads[:2])
            before = self.tree(self.workspace)
            with self.assertRaises(rw.RelatedWorksError):
                self.install(uploads[2])
            with self.assertRaises(rw.RelatedWorksError):
                rw.check_room(self.manifest_dir, 1)
            rw.check_room(self.manifest_dir, 0)
        self.assertEqual(self.tree(self.workspace), before)
        self.stage.peek([uploads[2]["token"]])
        self.assertEqual(str(rw.RelatedWorksError(f"A job can hold at most {rw.MAX_ITEMS} related works.")),
                         "A job can hold at most 200 related works.")

    def test_a_failure_while_linking_removes_what_was_placed(self):
        uploads = [self.upload("a.txt", b"a"), self.upload("b.txt", b"b")]
        place, calls = rw._place, []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError(errno.EIO, "Input/output error")
            return place(*args, **kwargs)

        with patch.object(rw, "_place", failing):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(*uploads)
        self.assertEqual(str(caught.exception), "Could not add the related works: [Errno 5] Input/output error")
        self.assertFalse(self.folder().exists())
        self.assertFalse(self.manifest_dir.exists())
        self.stage.peek(self.tokens(*uploads))

    def test_a_failure_after_index_md_restores_it_and_keeps_the_uploads(self):
        self.install(self.upload("kept.txt", b"kept"))
        index, manifest, tree = self.index(), self.manifest(), self.tree(self.workspace)
        uploads = [self.upload("new.txt", b"new"), self.stage.note("N", "note")]
        with patch.object(rw, "_write_manifest", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(*uploads)
        self.assertEqual(str(caught.exception), "Could not add the related works: [Errno 28] No space left on device")
        self.assertEqual((self.index(), self.manifest(), self.tree(self.workspace)), (index, manifest, tree))
        self.assertEqual(len(self.stage.peek(self.tokens(*uploads))), 2)
        self.assertEqual([entry["name"] for entry in self.install(*uploads)], ["new.txt", "N.md"])

    def test_record_runs_once_everything_is_in_place_and_its_failure_undoes_the_install(self):
        seen = []

        def record(entries):
            seen.append((entries, self.manifest()["items"][-len(entries):], self.index()))

        upload = self.upload("a.txt", b"abc")
        entries = self.install(upload, record=record)
        self.assertEqual(seen[0][0], entries)
        self.assertEqual(seen[0][1], entries)
        self.assertIn("1. a.txt: 3 B.", seen[0][2])
        raw, tree = (self.manifest_dir / rw.MANIFEST_NAME).read_bytes(), self.tree(self.workspace)
        second = self.upload("b.txt", b"b")
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.install(second, record=refuse)
        self.assertEqual(str(caught.exception), "Could not add the related works: audit log refused")
        self.assertEqual(((self.manifest_dir / rw.MANIFEST_NAME).read_bytes(), self.tree(self.workspace)), (raw, tree))
        workspace, manifest_dir = self.job("fresh")
        with self.assertRaises(rw.RelatedWorksError):
            self.install(second, workspace=workspace, manifest_dir=manifest_dir, record=refuse)
        self.assertEqual(os.listdir(workspace), [])
        self.assertFalse((manifest_dir / rw.MANIFEST_NAME).exists())
        self.stage.peek([second["token"]])

    def test_expired_duplicate_and_empty_token_lists(self):
        upload = self.upload("a.txt", b"abc")
        for tokens, message in (([upload["token"], "Z" * 24], rw.EXPIRED),
                                ([upload["token"], upload["token"]], "Each upload can be added only once.")):
            with self.subTest(tokens=tokens), self.assertRaises(rw.RelatedWorksError) as caught:
                rw.install(self.workspace, self.manifest_dir, tokens, self.stage, run_id="r")
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(rw.install(self.workspace, self.manifest_dir, [], self.stage, run_id="r"), [])
        self.assertEqual(os.listdir(self.workspace), [])

    def test_a_damaged_manifest_refuses_changes_and_lists_its_valid_entries(self):
        self.install(self.upload("a.txt", b"abc"))
        path = self.manifest_dir / rw.MANIFEST_NAME
        good = json.loads(path.read_text())
        good["items"].append({"name": "../escape", "kind": "file", "size": 1, "sha256": "0" * 64})
        unhashed = {**good["items"][0], "name": "b.txt", "sha256": ""}
        path.write_text(json.dumps(good))
        upload = self.upload("b.txt", b"b")
        for content in (json.dumps(good), "{not json", json.dumps({"schemaVersion": 2, "items": []}),
                        json.dumps({"schemaVersion": 1, "items": [unhashed]})):
            path.write_text(content)
            with self.subTest(content=content[:20]), self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload)
            self.assertEqual(str(caught.exception),
                             "The related-works record of this job is damaged; related works cannot be added.")
        path.write_text(json.dumps(good))
        self.assertEqual([row["name"] for row in rw.listing(self.workspace, self.manifest_dir)], ["a.txt"])
        path.write_text("{not json")
        self.assertEqual((rw.listing(self.workspace, self.manifest_dir), rw.count(self.workspace, self.manifest_dir)),
                         ([], 0))
        self.assertFalse((self.folder() / "b.txt").exists())

    def test_a_manifest_over_its_size_limit_is_damaged(self):
        self.install(self.upload("a.txt", b"abc"))
        path = self.manifest_dir / rw.MANIFEST_NAME
        size = path.stat().st_size
        with patch.object(rw, "MAX_MANIFEST_BYTES", size):
            self.assertEqual(rw.count(self.workspace, self.manifest_dir), 1)
            # Still valid JSON at its start: only the size tells.
            path.write_bytes(path.read_bytes() + b" " * 10)
            self.assertEqual(rw.count(self.workspace, self.manifest_dir), 0)
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(self.upload("b.txt", b"b"))
            self.assertEqual(str(caught.exception), rw.DAMAGED)

    def test_keep_staged_until_the_job_exists_and_never_share_an_upload_between_jobs(self):
        upload = self.upload("a.txt", b"abc")
        staged = self.stage.root / upload["token"] / "content"
        self.install(upload, keep_staged=True)
        self.assertEqual(inode(self.folder() / "a.txt"), inode(staged))
        self.stage.peek([upload["token"]])
        other, other_manifest = self.job("other")
        self.install(upload, workspace=other, manifest_dir=other_manifest)
        self.assertNotEqual(inode(self.folder(other) / "a.txt"), inode(self.folder() / "a.txt"))
        self.assertEqual((self.folder(other) / "a.txt").read_bytes(), b"abc")
        self.assertEqual(mode(self.folder(other) / "a.txt"), 0o600)
        self.assertFalse(staged.exists())
        self.assertEqual(os.lstat(self.folder() / "a.txt").st_nlink, 1)

    def test_a_file_system_without_links_gets_copies(self):
        upload = self.upload("a.txt", b"abc")
        staged = inode(self.stage.root / upload["token"] / "content")
        for code in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            with self.subTest(code=errno.errorcode[code]):
                workspace, manifest_dir = self.job(f"copy-{code}")
                with patch("os.link", side_effect=OSError(code, os.strerror(code))):
                    self.install(upload, workspace=workspace, manifest_dir=manifest_dir, keep_staged=True)
                copy = self.folder(workspace) / "a.txt"
                self.assertEqual((copy.read_bytes(), mode(copy), os.lstat(copy).st_nlink), (b"abc", 0o600, 1))
                self.assertNotEqual(inode(copy), staged)
        workspace, manifest_dir = self.job("refused")
        with patch("os.link", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload, workspace=workspace, manifest_dir=manifest_dir)
        self.assertIn("Permission denied", str(caught.exception))
        self.assertEqual(os.listdir(workspace), [])

    def test_an_upload_removed_during_the_install_has_expired(self):
        upload = self.upload("a.txt", b"abc")
        item = self.stage.get(upload["token"])
        with patch.object(self.stage, "peek", return_value=[item]):
            self.stage.discard(upload["token"])
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload)
        self.assertEqual(str(caught.exception),
                         "Could not add the related works: This upload has expired; add the file again.")
        self.assertFalse(self.folder().exists())

    def test_a_changed_staged_file_is_refused(self):
        upload = self.upload("a.txt", b"abc")
        item = self.stage.get(upload["token"])
        with patch.object(self.stage, "peek", return_value=[item]):
            (self.stage.root / upload["token"] / "content").write_bytes(b"abcd")
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(upload)
        self.assertIn("is not the file that was added", str(caught.exception))
        self.assertFalse(self.folder().exists())

    def test_without_descriptor_operations_the_folder_is_checked_by_path(self):
        with patch.object(rw, "DIR_FD", False):
            entries = self.install(self.upload("a.txt", b"abc"), self.stage.note("", "n"))
            self.assertEqual([entry["name"] for entry in entries], ["a.txt", "note.md"])
            self.assertEqual(self.index().splitlines()[-1], "2. note.md: note, 2 B.")
            entries = self.install(self.upload("A.TXT", b"x"))
            self.assertEqual(entries[0]["name"], "A-2.TXT")
            workspace, manifest_dir = self.job("linked")
            elsewhere = self.runs.parent / "outside"
            elsewhere.mkdir()
            self.folder(workspace).symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.install(self.upload("b.txt", b"b"), workspace=workspace, manifest_dir=manifest_dir)
            self.assertEqual(str(caught.exception), rw.NOT_PLAIN)
            self.assertEqual(os.listdir(elsewhere), [])
            folder = rw._Folder.at(self.workspace).child(rw.FOLDER)
            os.rename(self.folder(), self.workspace / "moved")
            self.folder().mkdir()
            with self.assertRaises(OSError):
                folder.lstat("a.txt")

    def test_installs_into_one_workspace_are_serialized(self):
        uploads = [self.upload("same.txt", bytes([n])) for n in range(12)]
        errors = []

        def run(upload):
            try:
                self.install(upload)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(upload,)) for upload in uploads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        names = [entry["name"] for entry in self.manifest()["items"]]
        self.assertEqual(sorted(names), sorted(["same.txt"] + [f"same-{n}.txt" for n in range(2, 13)]))
        self.assertEqual(len(self.index().splitlines()), 4 + 12)
        self.assertEqual(sorted((self.folder() / name).read_bytes() for name in names), [bytes([n]) for n in range(12)])

    def test_the_workspace_lock_is_reentrant(self):
        upload = self.upload("a.txt", b"abc")
        with rw.workspace_lock(self.workspace):
            with rw.workspace_lock(str(self.workspace) + "/"):
                self.install(upload)
        self.assertEqual(rw.count(self.workspace, self.manifest_dir), 1)

    def test_a_historical_workspace_gains_only_its_related_works_folder(self):
        workspace, manifest_dir = self.job("2026-10-27_11-00-00_cutoff")
        for name in (".codex", ".agents", ".git", ".aws", "prompts"):
            (workspace / name).mkdir()
        (workspace / "prompts" / "author.txt").write_text("prompt")
        for name, text in (("input.md", "statement"), ("draft.md", "draft"), ("checked-statement.md", "checked"),
                           ("job-settings.json", '{"retrievalPolicyPath": "/policy"}')):
            (workspace / name).write_text(text)
        before = self.tree(workspace)
        recorded = []
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            paper = self.upload("paper.pdf", b"%PDF-1.4 historical")
        entries = self.install(paper, self.stage.note("Hint", "text"), workspace=workspace, manifest_dir=manifest_dir,
                               run_id=workspace.name, record=recorded.extend)
        after = self.tree(workspace)
        self.assertEqual({key: value for key, value in after.items() if not key.startswith(rw.FOLDER)}, before)
        self.assertEqual(sorted(key for key in after if key not in before), [
            "related-works", "related-works/Hint.md", "related-works/INDEX.md", "related-works/paper.pdf",
            "related-works/paper.pdf.txt"])
        self.assertEqual(recorded, entries)
        audit = [{key: entry[key] for key in ("name", "size", "sha256", "kind", "addedAt", "textName", "textSha256")}
                 for entry in entries]
        self.assertEqual([(row["name"], row["size"], row["kind"]) for row in audit],
                         [("paper.pdf", 19, "file"), ("Hint.md", 13, "note")])
        self.assertEqual(audit[0]["sha256"], hashlib.sha256(b"%PDF-1.4 historical").hexdigest())
        self.assertEqual(audit[0]["textSha256"],
                         hashlib.sha256((workspace / rw.FOLDER / "paper.pdf.txt").read_bytes()).hexdigest())
        self.assertEqual(rw.audit_fields(entries[0]), {
            "name": "paper.pdf", "originalName": "paper.pdf", "kind": "file", "size": 19,
            "sha256": audit[0]["sha256"], "textName": "paper.pdf.txt", "textSize": entries[0]["textSize"],
            "textSha256": audit[0]["textSha256"], "addedAt": entries[0]["addedAt"], "addedIn": workspace.name,
            "batch": ""})
        json.dumps([rw.audit_fields(entry) for entry in entries])


class ListingNoticeTests(StoreCase):
    def test_listing_rows(self):
        with patch.object(rw, "_run_extraction", fake_extraction(4)):
            pdf = self.upload("Paper.pdf", b"%PDF-1.4 x")
        self.install(pdf, self.stage.note("T", "body"), self.upload("Odd name.txt", b"x" * 2048))
        rows = rw.listing(self.workspace, self.manifest_dir)
        added = rows[0]["addedAt"]
        self.assertEqual(rows, [
            {"name": "Paper.pdf", "originalName": "Paper.pdf", "kind": "file", "title": "", "sizeText": "10 B",
             "textStatus": "text extracted (4 pages)", "addedAt": added, "batch": ""},
            {"name": "T.md", "originalName": "", "kind": "note", "title": "T", "sizeText": "10 B", "textStatus": "",
             "addedAt": added, "batch": ""},
            {"name": "Odd-name.txt", "originalName": "Odd name.txt", "kind": "file", "title": "", "sizeText": "2.0 KB",
             "textStatus": "", "addedAt": added, "batch": ""},
        ])
        self.assertEqual(rw.count(self.workspace, self.manifest_dir), 3)
        other, other_manifest = self.job("empty")
        self.assertEqual((rw.listing(other, other_manifest), rw.count(other, other_manifest)), ([], 0))

    def test_exact_notice_and_its_long_fallback(self):
        self.assertEqual(rw.notice_text(["paper.pdf", "Lemma-3.md"]), (
            "RELATED WORKS ADDED:\nThe user added paper.pdf, Lemma-3.md to the related-works folder of your working "
            "directory; related-works/INDEX.md lists every related work. Read what is relevant."))
        self.assertEqual(rw.notice_text([]), "")
        names = ["n" * 98 + f"{index:02d}" for index in range(100)]
        self.assertEqual(rw.notice_text(names), (
            "RELATED WORKS ADDED:\nThe user added 100 files to the related-works folder of your working directory; "
            "related-works/INDEX.md lists every related work. Read what is relevant."))
        exact = ["x" * 9997, "y"]  # Joined: exactly 10,000 characters.
        self.assertTrue(rw.notice_text(exact).startswith("RELATED WORKS ADDED:\nThe user added " + "x" * 9997
                                                         + ", y to the related-works folder"))
        self.assertLessEqual(len(rw.notice_text(exact)), 12000)
        self.assertIn("The user added 2 files to", rw.notice_text(["x" * 9998, "y"]))

    def test_describe_entry(self):
        base = {"size": 1258291, "originalName": "", "title": "", "mediaType": "", "textName": "", "pages": 0,
                "textProblem": ""}
        cases = [
            ({**base, "name": "data.csv", "kind": "file", "originalName": "data.csv"}, "data.csv (1.2 MB)"),
            ({**base, "name": "paper.pdf", "kind": "file", "mediaType": "application/pdf", "textName": "paper.pdf.txt",
              "pages": 12}, "paper.pdf (PDF, 1.2 MB, text extracted (12 pages))"),
            ({**base, "name": "My-Paper.pdf", "kind": "file", "mediaType": "application/pdf",
              "textProblem": rw.PROBLEM_ENCRYPTED, "originalName": "My Paper.pdf"},
             'My-Paper.pdf (PDF, 1.2 MB, no extracted text: the PDF is encrypted, uploaded as "My Paper.pdf")'),
            ({**base, "name": "Idea.md", "kind": "note", "title": "Idea", "size": 20}, 'Idea.md (note "Idea", 20 B)'),
            ({**base, "name": "note.md", "kind": "note", "size": 20}, "note.md (note, 20 B)"),
        ]
        for entry, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(rw.describe_entry(entry), expected)

    def test_index_and_display_text_stay_on_one_line(self):
        entry = {"name": "a.txt", "kind": "file", "size": 1, "originalName": "two\nlines\u2028here.txt"}
        self.assertEqual(rw.index_text([entry]).splitlines()[-1], '1. a.txt: 1 B. Uploaded as "two lines here.txt".')
        self.assertNotIn("\n", rw.describe_entry(entry))
        note = {"name": "n.md", "kind": "note", "size": 1, "title": "two\nlines\rhere"}
        self.assertEqual(rw.index_text([note]).splitlines()[-1], '1. n.md: note "two lines here", 1 B.')
        self.assertNotIn("\n", rw.describe_entry(note))


class CopyIntoTests(StoreCase):
    def test_review_retry_copies_regular_files_only_and_never_links(self):
        with patch.object(rw, "_run_extraction", fake_extraction(2)):
            pdf = self.upload("paper.pdf", b"%PDF-1.4 paper")
        self.install(pdf, self.upload("linked.txt", b"secret?"), self.upload("changed.txt", b"before"),
                     self.stage.note("N", "note text"))
        source = self.folder()
        outside = self.runs.parent / "outside.txt"
        outside.write_bytes(b"secret?")
        (source / "linked.txt").unlink()
        (source / "linked.txt").symlink_to(outside)
        (source / "changed.txt").write_bytes(b"after!")
        target, target_manifest = self.job("retry")
        copied = rw.copy_into(self.workspace, self.manifest_dir, target, target_manifest, run_id="retry")
        self.assertEqual(copied, 2)
        folder = self.folder(target)
        self.assertEqual(sorted(os.listdir(folder)), ["INDEX.md", "N.md", "paper.pdf", "paper.pdf.txt"])
        for name in ("paper.pdf", "paper.pdf.txt", "N.md"):
            with self.subTest(name=name):
                self.assertNotEqual(inode(folder / name), inode(source / name))
                self.assertEqual((folder / name).read_bytes(), (source / name).read_bytes())
                self.assertEqual((mode(folder / name), os.lstat(folder / name).st_nlink), (0o600, 1))
        entries = self.manifest(target_manifest)["items"]
        self.assertEqual([(entry["name"], entry["addedIn"]) for entry in entries],
                         [("paper.pdf", "retry"), ("N.md", "retry")])
        self.assertEqual(self.index(target).splitlines()[-2:], [
            "1. paper.pdf: PDF, 14 B, 2 pages; text in paper.pdf.txt.", '2. N.md: note "N", 15 B.'])
        nothing, nothing_manifest = self.job("nothing")
        self.assertEqual(rw.copy_into(nothing, nothing_manifest, target, target_manifest, run_id="x"), 0)

    def test_a_linked_source_folder_copies_nothing(self):
        self.install(self.upload("a.txt", b"abc"))
        os.rename(self.folder(), self.workspace / "elsewhere")
        self.folder().symlink_to(self.workspace / "elsewhere", target_is_directory=True)
        target, target_manifest = self.job("retry")
        self.assertEqual(rw.copy_into(self.workspace, self.manifest_dir, target, target_manifest, run_id="retry"), 0)
        self.assertEqual(os.listdir(target), [])

    def test_a_source_that_grows_during_the_copy_is_cut_off_at_its_size(self):
        size = 2 * rw.BLOCK_BYTES
        self.install(self.upload("a.txt", b"\xff" * size), self.upload("b.txt", b"bbb"))
        source, write, copied = self.folder() / "a.txt", rw._write_all, []

        def growing(descriptor, data):
            if bytes(data[:1]) == b"\xff":
                if not copied:
                    with source.open("ab") as stream:  # The source job's author appends to its file meanwhile.
                        stream.write(b"\xff" * (3 * size))
                copied.append(len(data))
            return write(descriptor, data)

        target, target_manifest = self.job("retry")
        with patch.object(rw, "_write_all", growing):
            self.assertEqual(rw.copy_into(self.workspace, self.manifest_dir, target, target_manifest, run_id="retry"), 1)
        self.assertLessEqual(sum(copied), size)
        self.assertEqual(sorted(os.listdir(self.folder(target))), ["INDEX.md", "b.txt"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are POSIX only")
    def test_a_fifo_in_the_source_folder_is_skipped_without_waiting(self):
        self.install(self.upload("a.txt", b"abc"), self.upload("b.txt", b"bbb"))
        fifo = self.folder() / "a.txt"
        fifo.unlink()
        os.mkfifo(fifo)  # The source job's author replaces its file with a FIFO.
        target, target_manifest = self.job("retry")
        result = {}
        thread = threading.Thread(daemon=True, target=lambda: result.update(count=rw.copy_into(
            self.workspace, self.manifest_dir, target, target_manifest, run_id="retry")))
        thread.start()
        thread.join(10)
        if thread.is_alive():
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # Let the blocked reader go before failing.
            thread.join(5)
            self.fail("copy_into waited for a writer on a FIFO.")
        self.assertEqual(result["count"], 1)
        self.assertEqual(sorted(os.listdir(self.folder(target))), ["INDEX.md", "b.txt"])
        rw.install(target, target_manifest, self.tokens(self.upload("c.txt", b"c")), self.stage, run_id="retry")
        self.assertEqual(rw.count(target, target_manifest), 2)

    def test_items_of_another_batch_are_copied_from_the_source_not_linked(self):
        controller = self.runs / ".controller"
        first = rw.BatchStore(rw.batch_directory(controller, "b_00000000a1"))
        second = rw.BatchStore(rw.batch_directory(controller, "b_00000000b2"))
        first.install(self.tokens(self.upload("shared.txt", b"first batch")), self.stage)
        second.install(self.tokens(self.upload("shared.txt", b"other batch")), self.stage)
        first.link_into(self.workspace, self.manifest_dir, run_id="source")
        target, target_manifest = self.job("retry")
        self.assertEqual(rw.copy_into(self.workspace, self.manifest_dir, target, target_manifest, run_id="retry",
                                      batch=second), 1)
        copy = self.folder(target) / "shared.txt"
        self.assertEqual(copy.read_bytes(), b"first batch")
        self.assertNotIn(inode(copy), (inode(first.root / rw.FOLDER / "shared.txt"),
                                       inode(second.root / rw.FOLDER / "shared.txt")))
        entry = self.manifest(target_manifest)["items"][0]
        self.assertEqual((entry["batch"], entry["batchName"]), (first.id, "shared.txt"))


class BatchStoreTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.controller = self.runs / ".controller"
        self.batch = rw.BatchStore(rw.batch_directory(self.controller, rw.new_batch_id()))
        self.jobs = [self.job(f"2026-10-27_12-00-0{n}_batch") for n in range(3)]

    def shared(self, name):
        return self.batch.root / rw.FOLDER / name

    def test_batch_ids_and_directories(self):
        self.assertRegex(rw.new_batch_id(), r"\Ab_[0-9a-f]{10}\Z")
        self.assertEqual(rw.batch_directory(self.controller, "b_0123456789"),
                         self.controller / ".batches" / "b_0123456789")
        for bad in ("..", ".", "../x", "a/b", "", ".hidden", "x" * 65, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rw.batch_directory(self.controller, bad)
        with self.assertRaises(ValueError):
            rw.BatchStore(self.controller / "..")

    def test_the_batch_store_holds_one_copy_hard_linked_into_every_job(self):
        with patch.object(rw, "_run_extraction", fake_extraction(5)):
            pdf = self.upload("paper.pdf", b"%PDF-1.4 shared")
        installed = self.batch.install(self.tokens(pdf, self.stage.note("Plan", "steps")), self.stage)
        self.assertEqual([entry["name"] for entry in installed], ["paper.pdf", "Plan.md"])
        self.assertEqual(installed[0]["addedIn"], self.batch.id)
        self.assertEqual([mode(self.batch.root), mode(self.batch.root / rw.FOLDER),
                          mode(self.batch.root / rw.MANIFEST_NAME), mode(self.shared(rw.INDEX_NAME))],
                         [0o700, 0o700, 0o600, 0o600])
        # Shared by every job's hard link: read-only, so no author can change another job's copy in place.
        self.assertEqual([mode(self.shared(name)) for name in ("paper.pdf", "paper.pdf.txt", "Plan.md")],
                         [0o400, 0o400, 0o400])
        self.assertTrue((self.batch.root / rw.FOLDER / rw.INDEX_NAME).read_text().endswith(
            '2. Plan.md: note "Plan", 14 B.\n'))
        self.assertEqual(os.listdir(self.stage.root), [])
        for workspace, manifest_dir in self.jobs:
            result = self.batch.link_into(workspace, manifest_dir, run_id=workspace.name)
            self.assertEqual(([entry["name"] for entry in result["entries"]], result["copied"], result["skipped"],
                              result["diagnostic"]), (["paper.pdf", "Plan.md"], [], [], ""))
        for name in ("paper.pdf", "paper.pdf.txt", "Plan.md"):
            with self.subTest(name=name):
                self.assertEqual({inode(self.folder(workspace) / name) for workspace, _ in self.jobs},
                                 {inode(self.shared(name))})
                self.assertEqual(os.lstat(self.shared(name)).st_nlink, 4)
                with self.assertRaises(PermissionError):
                    open(self.folder(self.jobs[1][0]) / name, "ab").close()
        workspace, manifest_dir = self.jobs[0]
        self.assertEqual(self.index(workspace), (
            "# Related works\n\nFiles the user supplied for this problem, oldest first.\n\n"
            "1. paper.pdf: PDF, 15 B, 5 pages; text in paper.pdf.txt.\n"
            '2. Plan.md: note "Plan", 14 B.\n'))
        self.assertNotEqual(inode(self.folder(workspace) / rw.INDEX_NAME), inode(self.shared(rw.INDEX_NAME)))
        entry = self.manifest(manifest_dir)["items"][0]
        self.assertEqual((entry["batch"], entry["batchName"], entry["addedIn"]),
                         (self.batch.id, "paper.pdf", workspace.name))
        self.assertEqual(rw.listing(workspace, manifest_dir)[0]["batch"], self.batch.id)
        self.assertEqual([row["name"] for row in self.batch.listing()], ["paper.pdf", "Plan.md"])
        self.assertEqual(self.batch.count(), 2)

    def test_additions_reach_every_job_once_with_job_specific_items_kept(self):
        self.batch.install(self.tokens(self.upload("paper.pdf", b"batch paper")), self.stage)
        own, own_manifest = self.jobs[0]
        rw.install(own, own_manifest, self.tokens(self.upload("paper.pdf", b"own paper")), self.stage, run_id="own")
        for workspace, manifest_dir in self.jobs:
            self.batch.link_into(workspace, manifest_dir, run_id=workspace.name)
        added = self.batch.install(self.tokens(self.upload("extra.md", b"# extra\n")), self.stage)
        notices = []
        for workspace, manifest_dir in self.jobs:
            result = self.batch.link_into(workspace, manifest_dir, run_id=workspace.name,
                                          names=[entry["name"] for entry in added])
            notices.append(rw.notice_text([entry["name"] for entry in result["entries"]]))
            self.assertEqual(self.batch.link_into(workspace, manifest_dir, run_id=workspace.name),
                             {"entries": [], "copied": [], "skipped": [], "diagnostic": ""})
        self.assertEqual(len(notices), 3)
        self.assertTrue(all("The user added extra.md to the related-works folder" in notice for notice in notices))
        self.assertEqual(self.index(own).splitlines()[4:], [
            "1. paper.pdf: 9 B.", '2. paper-2.pdf: 11 B. Uploaded as "paper.pdf".', "3. extra.md: 8 B."])
        self.assertEqual((self.folder(own) / "paper.pdf").read_bytes(), b"own paper")
        self.assertEqual(inode(self.folder(own) / "paper-2.pdf"), inode(self.shared("paper.pdf")))
        self.assertEqual(self.index(self.jobs[1][0]).splitlines()[4:], ["1. paper.pdf: 11 B.", "2. extra.md: 8 B."])
        self.assertEqual({inode(self.folder(workspace) / "extra.md") for workspace, _ in self.jobs},
                         {inode(self.shared("extra.md"))})
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.batch.link_into(own, own_manifest, run_id="own", names=["missing.pdf"])
        self.assertEqual(str(caught.exception), "The batch has no related work named missing.pdf.")

    def test_a_job_that_cannot_link_gets_a_copy_and_a_diagnostic(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"aaa"), self.upload("b.txt", b"bbb")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        for code, reason in ((errno.EXDEV, "cross-device link"), (errno.EPERM, "operation not permitted")):
            with self.subTest(reason=reason):
                workspace, manifest_dir = self.job(f"copy-{code}")
                with patch("os.link", side_effect=OSError(code, os.strerror(code))):
                    result = self.batch.link_into(workspace, manifest_dir, run_id=workspace.name)
                self.assertEqual(result["copied"], ["a.txt", "b.txt"])
                self.assertEqual(result["diagnostic"], (
                    "The batch's related works could not be hard-linked into this job's related-works folder "
                    f"({reason}), so this job has its own copy of a.txt, b.txt, which uses extra disk space."))
                copy = self.folder(workspace) / "a.txt"
                self.assertNotEqual(inode(copy), inode(self.shared("a.txt")))
                self.assertEqual((copy.read_bytes(), mode(copy), mode(self.shared("a.txt"))), (b"aaa", 0o600, 0o400))
                self.assertEqual([entry["name"] for entry in self.manifest(manifest_dir)["items"]], ["a.txt", "b.txt"])
        linked, linked_manifest = self.jobs[1]
        self.assertEqual(self.batch.link_into(linked, linked_manifest, run_id="linked")["copied"], [])
        self.assertEqual(os.lstat(self.shared("a.txt")).st_nlink, 2)

    def test_a_long_copy_diagnostic_names_the_count(self):
        uploads = [self.upload(f"{'n' * 110}{index:03d}.txt", b"x") for index in range(100)]
        self.batch.install(self.tokens(*uploads), self.stage)
        workspace, manifest_dir = self.jobs[0]
        with patch("os.link", side_effect=OSError(errno.EXDEV, os.strerror(errno.EXDEV))):
            result = self.batch.link_into(workspace, manifest_dir, run_id="job")
        self.assertEqual(len(result["copied"]), 100)
        self.assertIn("so this job has its own copy of 100 files, which uses extra disk space.", result["diagnostic"])

    def test_the_item_limit_applies_to_each_job(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"a"), self.upload("b.txt", b"b")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        rw.install(workspace, manifest_dir, self.tokens(self.upload("own.txt", b"o")), self.stage, run_id="own")
        before = self.tree(workspace)
        with patch.object(rw, "MAX_ITEMS", 2):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.batch.link_into(workspace, manifest_dir, run_id="own")
            self.assertEqual(str(caught.exception), "A job can hold at most 2 related works.")
            with self.assertRaises(rw.RelatedWorksError):
                self.batch.install(self.tokens(self.upload("c.txt", b"c")), self.stage)
        self.assertEqual(self.tree(workspace), before)

    def test_a_damaged_or_linked_store_is_refused(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"a")), self.stage)
        folder = self.batch.root / rw.FOLDER
        os.rename(folder, self.batch.root / "moved")
        folder.symlink_to(self.batch.root / "moved", target_is_directory=True)
        workspace, manifest_dir = self.jobs[0]
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.batch.link_into(workspace, manifest_dir, run_id="job")
        self.assertEqual(str(caught.exception),
                         "The related-works folder of this batch is not a plain folder; related works cannot be added.")
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.batch.install(self.tokens(self.upload("b.txt", b"b")), self.stage)
        self.assertEqual(str(caught.exception), rw.BATCH_NOT_PLAIN)
        self.assertEqual(os.listdir(workspace), [])

    def test_a_batch_install_that_cannot_make_its_files_read_only_is_undone(self):
        upload = self.upload("a.txt", b"abc")
        with patch("os.fchmod", side_effect=PermissionError(errno.EPERM, "Operation not permitted")):
            with self.assertRaises(rw.RelatedWorksError) as caught:
                self.batch.install(self.tokens(upload), self.stage)
        self.assertEqual(str(caught.exception), "Could not add the related works: [Errno 1] Operation not permitted")
        self.assertEqual((self.batch.count(), os.listdir(self.batch.root)), (0, []))
        self.stage.peek(self.tokens(upload))
        self.assertEqual([entry["name"] for entry in self.batch.install(self.tokens(upload), self.stage)], ["a.txt"])

    def edit_through_a_link(self, path, data, *, read_only=True):
        """What an author can do to its own link of a shared file: make it writable, write, and maybe chmod back."""

        os.chmod(path, 0o600)
        path.write_bytes(data)
        if read_only:
            os.chmod(path, 0o400)

    def test_a_shared_file_edited_through_one_job_is_left_out_of_later_jobs(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"Lemma 1 is TRUE.\n"), self.upload("b.txt", b"bbb")),
                           self.stage)
        first, second, later = self.jobs
        for workspace, manifest_dir in (first, second):
            self.batch.link_into(workspace, manifest_dir, run_id=workspace.name)
        # The same size, and read-only again: only the bytes tell.
        self.edit_through_a_link(self.folder(first[0]) / "a.txt", b"Lemma 1 is FALS.\n")
        # Jobs already linked share the inode; nothing in the store can take that edit back from them.
        self.assertEqual((self.folder(second[0]) / "a.txt").read_bytes(), b"Lemma 1 is FALS.\n")
        result = self.batch.link_into(*later, run_id="later")
        self.assertEqual(([entry["name"] for entry in result["entries"]], result["copied"], result["skipped"]),
                         (["b.txt"], [], ["a.txt"]))
        self.assertEqual(result["diagnostic"],
                         "The batch's copy of a.txt no longer matches what was added, so this job did not get it.")
        self.assertEqual(sorted(os.listdir(self.folder(later[0]))), ["INDEX.md", "b.txt"])
        self.assertEqual(self.index(later[0]).splitlines()[4:], ["1. b.txt: 3 B."])
        for batch in (self.batch, None):
            with self.subTest(batch=batch):
                retry, retry_manifest = self.job(f"retry-{batch is None}")
                self.assertEqual(rw.copy_into(first[0], first[1], retry, retry_manifest, run_id="retry", batch=batch), 1)
                self.assertEqual(sorted(os.listdir(self.folder(retry))), ["INDEX.md", "b.txt"])

    def test_a_shared_file_of_another_size_leaves_out_only_that_item(self):
        self.batch.install(self.tokens(*(self.upload(name, name.encode()) for name in ("a.txt", "b.txt", "c.txt"))),
                           self.stage)
        first, later = self.jobs[0], self.jobs[1]
        self.batch.link_into(*first, run_id="first")
        self.edit_through_a_link(self.folder(first[0]) / "b.txt", b"changed!", read_only=False)
        result = self.batch.link_into(*later, run_id="later")
        self.assertEqual(([entry["name"] for entry in result["entries"]], result["skipped"]), (["a.txt", "c.txt"], ["b.txt"]))
        self.assertEqual(rw.count(*later), 2)
        self.edit_through_a_link(self.folder(first[0]) / "c.txt", b"also changed")
        result = self.batch.link_into(*self.jobs[2], run_id="third")
        self.assertEqual(([entry["name"] for entry in result["entries"]], result["skipped"]), (["a.txt"], ["b.txt", "c.txt"]))
        self.assertEqual(result["diagnostic"], "The batch's copies of b.txt, c.txt no longer match what was added, so "
                                               "this job did not get them.")

    def test_a_shared_file_made_writable_but_unchanged_is_copied(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"abc")), self.stage)
        os.chmod(self.shared("a.txt"), 0o644)  # An author ran chmod on its link, and wrote nothing.
        workspace, manifest_dir = self.jobs[0]
        result = self.batch.link_into(workspace, manifest_dir, run_id="job")
        self.assertEqual(([entry["name"] for entry in result["entries"]], result["copied"], result["skipped"]),
                         (["a.txt"], ["a.txt"], []))
        self.assertEqual(result["diagnostic"], (
            "The batch's related works could not be hard-linked into this job's related-works folder (a shared file "
            "is no longer read-only), so this job has its own copy of a.txt, which uses extra disk space."))
        copy = self.folder(workspace) / "a.txt"
        self.assertNotEqual(inode(copy), inode(self.shared("a.txt")))
        self.assertEqual((copy.read_bytes(), mode(copy), os.lstat(copy).st_nlink), (b"abc", 0o600, 1))

    def test_a_shared_file_is_hashed_again_only_after_it_changes(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"abc"), self.upload("b.txt", b"bbb")), self.stage)
        digest, hashed = rw._digest, []

        def counting(descriptor):
            hashed.append(os.fstat(descriptor).st_size)
            return digest(descriptor)

        rw._checked.clear()
        with patch.object(rw, "_digest", counting):
            for workspace, manifest_dir in self.jobs[:2]:
                self.batch.link_into(workspace, manifest_dir, run_id=workspace.name)
            self.assertEqual(len(hashed), 2)  # Once per file: the second job's links reuse the check.
            path = self.shared("a.txt")
            before = os.stat(path)
            self.edit_through_a_link(path, b"xyz")  # The same size,
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the old modification time.
            result = self.batch.link_into(*self.jobs[2], run_id="third")
        self.assertEqual(len(hashed), 3)
        self.assertEqual(result["skipped"], ["a.txt"])

    def test_a_job_never_gets_an_item_of_a_batch_install_that_is_undone(self):
        upload = self.upload("a.txt", b"abc")
        reached, linked, errors = threading.Event(), threading.Event(), []

        def failing(folder, entries):
            reached.set()
            linked.wait(5)  # link_into runs for a job here, while the batch install is under way.
            raise OSError(errno.EPERM, "Operation not permitted")

        def install():
            try:
                self.batch.install(self.tokens(upload), self.stage)
            except rw.RelatedWorksError as exc:
                errors.append(str(exc))

        workspace, manifest_dir = self.jobs[0]
        with patch.object(rw.BatchStore, "_read_only", staticmethod(failing)):
            thread = threading.Thread(target=install)
            thread.start()
            self.assertTrue(reached.wait(5))
            result = self.batch.link_into(workspace, manifest_dir, run_id="job")
            linked.set()
            thread.join()
        self.assertEqual(errors, ["Could not add the related works: [Errno 1] Operation not permitted"])
        self.assertEqual((result["entries"], rw.count(workspace, manifest_dir), self.batch.count()), ([], 0, 0))
        added = self.batch.install(self.tokens(upload), self.stage)
        result = self.batch.link_into(workspace, manifest_dir, run_id="job", names=[added[0]["name"]])
        self.assertEqual([entry["name"] for entry in result["entries"]], ["a.txt"])
        self.assertEqual(inode(self.folder(workspace) / "a.txt"), inode(self.shared("a.txt")))

    def test_names_limit_which_batch_items_a_job_gets(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"a"), self.upload("b.txt", b"b")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        result = self.batch.link_into(workspace, manifest_dir, run_id="job", names=["b.txt"])
        self.assertEqual([entry["name"] for entry in result["entries"]], ["b.txt"])
        self.assertEqual(sorted(os.listdir(self.folder(workspace))), ["INDEX.md", "b.txt"])
        self.assertEqual([entry["name"] for entry in self.batch.link_into(workspace, manifest_dir,
                                                                           run_id="job")["entries"]], ["a.txt"])

    def test_a_damaged_job_record_refuses_links(self):
        self.batch.install(self.tokens(self.upload("a.txt", b"a")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        rw.install(workspace, manifest_dir, self.tokens(self.upload("own.txt", b"o")), self.stage, run_id="own")
        path = manifest_dir / rw.MANIFEST_NAME
        path.write_text("{damaged")
        before = self.tree(workspace)
        with self.assertRaises(rw.RelatedWorksError) as caught:
            self.batch.link_into(workspace, manifest_dir, run_id="job")
        self.assertEqual(str(caught.exception), rw.DAMAGED)
        self.assertEqual((path.read_text(), self.tree(workspace)), ("{damaged", before))

    def test_review_retry_and_continuations_keep_the_links(self):
        self.batch.install(self.tokens(self.upload("shared.txt", b"shared")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        self.batch.link_into(workspace, manifest_dir, run_id="first")
        rw.install(workspace, manifest_dir, self.tokens(self.upload("own.txt", b"own")), self.stage, run_id="first")
        retry, retry_manifest = self.job("retry")
        self.assertEqual(rw.copy_into(workspace, manifest_dir, retry, retry_manifest, run_id="retry",
                                      batch=self.batch), 2)
        self.assertEqual(inode(self.folder(retry) / "shared.txt"), inode(self.shared("shared.txt")))
        self.assertNotEqual(inode(self.folder(retry) / "own.txt"), inode(self.folder(workspace) / "own.txt"))
        entries = self.manifest(retry_manifest)["items"]
        self.assertEqual([(entry["name"], entry.get("batch", "")) for entry in entries],
                         [("shared.txt", self.batch.id), ("own.txt", "")])
        added = self.batch.install(self.tokens(self.upload("later.txt", b"later")), self.stage)
        result = self.batch.link_into(retry, retry_manifest, run_id="retry", names=[added[0]["name"]])
        self.assertEqual([entry["name"] for entry in result["entries"]], ["later.txt"])
        self.assertEqual(self.batch.link_into(retry, retry_manifest, run_id="retry")["entries"], [])
        unlinked, unlinked_manifest = self.job("without-store")
        self.assertEqual(rw.copy_into(workspace, manifest_dir, unlinked, unlinked_manifest, run_id="x"), 2)
        self.assertNotEqual(inode(self.folder(unlinked) / "shared.txt"), inode(self.shared("shared.txt")))
        self.assertEqual(self.manifest(unlinked_manifest)["items"][0]["batch"], self.batch.id)
        self.assertEqual([entry["name"] for entry in self.batch.link_into(
            unlinked, unlinked_manifest, run_id="x")["entries"]], ["later.txt"])

    def test_a_review_retry_never_doubles_a_batch_item_the_job_already_has(self):
        self.batch.install(self.tokens(self.upload("shared.txt", b"shared")), self.stage)
        workspace, manifest_dir = self.jobs[0]
        self.batch.link_into(workspace, manifest_dir, run_id="first")
        retry, retry_manifest = self.job("retry")
        self.batch.link_into(retry, retry_manifest, run_id="retry")
        for batch in (self.batch, None):
            with self.subTest(batch=batch):
                self.assertEqual(rw.copy_into(workspace, manifest_dir, retry, retry_manifest, run_id="retry",
                                              batch=batch), 0)
        self.assertEqual(sorted(os.listdir(self.folder(retry))), ["INDEX.md", "shared.txt"])
        with patch.object(rw, "MAX_ITEMS", 1):
            self.assertEqual(rw.copy_into(workspace, manifest_dir, retry, retry_manifest, run_id="retry"), 0)

    def test_without_descriptor_operations_links_still_share_the_inode(self):
        with patch.object(rw, "DIR_FD", False):
            self.batch.install(self.tokens(self.upload("a.txt", b"abc")), self.stage)
            workspace, manifest_dir = self.jobs[0]
            self.assertEqual(self.batch.link_into(workspace, manifest_dir, run_id="job")["copied"], [])
        self.assertEqual(inode(self.folder(workspace) / "a.txt"), inode(self.shared("a.txt")))


if __name__ == "__main__":
    unittest.main()
