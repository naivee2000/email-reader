from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def provider_oauth_client_readiness(vault) -> tuple[bool, bool]:
    """Read only OAuth client presence, never tokens or private mail."""
    google_id = bool(vault.get("google-client-id"))
    google_secret = bool(vault.get("google-client-secret"))
    microsoft_id = bool(vault.get("microsoft-client-id"))
    return google_id and google_secret, microsoft_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private multi-account email assistant")
    parser.add_argument("--demo", action="store_true", help="Use fictional local demo data")
    parser.add_argument("--check-storage", action="store_true", help="Verify encrypted database integrity without provider or AI requests")
    parser.add_argument(
        "--check-access",
        action="store_true",
        help="Verify private-data access without opening mail or syncing",
    )
    parser.add_argument(
        "--check-provider-setup",
        action="store_true",
        help="Report whether OAuth client configuration exists; never open mail or sign in",
    )
    parser.add_argument(
        "--sync-once",
        action="store_true",
        help="Run one real account refresh and exit (diagnostics)",
    )
    parser.add_argument("--sync-health", action="store_true", help="Read aggregate sync dates and counts only; no message text, credentials, or provider changes")
    parser.add_argument("--inspect-import", metavar="SUBJECT", help="Diagnose a recent subject across Mail and local storage; report only header flags and counts")
    parser.add_argument("--recover-import", metavar="SUBJECT", help="Recover matching recent Inbox originals locally; no provider changes")
    parser.add_argument("--import-account-number", type=int, help="Limit the import diagnostic/recovery to one numbered account")
    parser.add_argument(
        "--audit-sources",
        action="store_true",
        help="Check the two requested insurance files without printing private contents",
    )
    parser.add_argument("--audit-search", action="store_true", help="Check insurance search; keep answer text encrypted")
    parser.add_argument("--audit-search-suite", action="store_true", help="Check ten fixed real-mail questions; keep answers and sources encrypted")
    parser.add_argument("--search-suite-report", action="store_true", help="Read only aggregate source/file ranks from the latest encrypted search evaluation")
    parser.add_argument("--audit-local-retrieval", action="store_true", help="Check two fixed local retrieval probes without AI; print only aggregate ranks")
    parser.add_argument("--reindex-local-pdfs", action="store_true", help="Rebuild one batch of PDF text from encrypted local originals; no provider reads")
    parser.add_argument("--reindex-local-text", action="store_true", help="Rebuild one batch of text/CSV/TSV from encrypted local originals; no provider reads")
    parser.add_argument("--reindex-local-office", action="store_true", help="Rebuild one batch of Word/Excel/PowerPoint text from encrypted local originals; no provider reads")
    parser.add_argument("--reindex-limit", type=int, default=20, help="Local extraction reindex batch size, 1–100")
    parser.add_argument("--reply-cohort-availability", action="store_true", help="Report only whether the fixed encrypted seed has ten eligible reply threads")
    parser.add_argument("--benchmark-read-open", action="store_true", help="Time ten local read transitions on an encrypted copy; never change provider mail")
    parser.add_argument("--freeze-rendering-reference-set", action="store_true", help="Save twelve real visual-check conversations in encrypted local storage")
    parser.add_argument(
        "--audit-observed-snapshot-case", type=int, choices=range(1, 51),
        help="Check one encrypted historical reply snapshot without printing mail",
    )
    parser.add_argument(
        "--benchmark-replies",
        choices=["before", "after", "luna", "shared-tone", "single-pass"],
        help="Measure ten historical replies; store results only in encrypted diagnostics",
    )
    parser.add_argument(
        "--benchmark-label",
        default="",
        help="Short unique label for a new encrypted reply benchmark; never overwrites earlier runs",
    )
    parser.add_argument(
        "--benchmark-effort",
        choices=["none", "low"],
        default="low",
        help="Diagnostic-only reply reasoning effort; does not change app settings",
    )
    parser.add_argument(
        "--benchmark-compact", action="store_true",
        help="Diagnostic-only compact reply schema; does not change app settings",
    )
    parser.add_argument(
        "--benchmark-cohort", choices=["legacy", "actionable", "recent", "recent-dialogue", "observed"], default="legacy",
        help="Diagnostic-only reply selection; observed reviews real incoming-to-sent pairs",
    )
    parser.add_argument(
        "--benchmark-require-observed-intents", action="store_true",
        help="Require private, human-supplied intent for each observed reply case",
    )
    parser.add_argument(
        "--review-reply-benchmarks", nargs=2, metavar=("LOW_LABEL", "FAST_LABEL"),
        help="Report only structural comparisons of two encrypted ten-thread reply runs",
    )
    parser.add_argument(
        "--review-reply-case", type=int, choices=range(1, 11),
        help="Show one benchmark case in a local read-only review window; requires two labels",
    )
    parser.add_argument(
        "--review-reply-cohort-case", type=int, choices=range(1, 51),
        help="Show one selected reply-cohort source in a local read-only window without AI",
    )
    parser.add_argument(
        "--annotate-observed-intent", action="store_true",
        help="Add an encrypted intent note to an observed-cohort review window",
    )
    parser.add_argument(
        "--review-search-case", type=int, choices=range(1, 11),
        help="Show one saved search answer and cited excerpts in a local read-only window",
    )
    parser.add_argument(
        "--review-rendering-gaps", action="store_true",
        help="Show up to twelve unlinked rendered-mail examples in a local read-only window",
    )
    return parser


def qml_entrypoint() -> Path:
    candidates = [Path(__file__).parent / "qml" / "Main.qml"]
    if getattr(sys, "frozen", False):
        candidates.extend(
            [
                Path(sys.executable).resolve().parent.parent
                / "Resources"
                / "email_reader"
                / "qml"
                / "Main.qml",
                Path(getattr(sys, "_MEIPASS", "")) / "email_reader" / "qml" / "Main.qml",
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def startup_error_message(error: str) -> str:
    if "keychain" in error.casefold() or "password" in error.casefold():
        return (
            "Email Reader could not unlock its encrypted data. If you cancelled the macOS "
            "Keychain dialog, reopen the app, enter your Mac password there, and choose Allow. "
            "EmailReaderKeychain is the app's access helper. It keeps a stable identity across "
            "updates. Your password is never entered into Email Reader."
        )
    return "Email Reader could not open its private data. No email was changed."


def startup_error_qml(error: str) -> bytes:
    """Dismissible failure window that never interpolates raw diagnostics."""
    return f"""import QtQuick
import QtQuick.Controls
ApplicationWindow {{
    visible: true; width: 560; height: Math.max(260, errorContents.implicitHeight + 56); title: "Email Reader"
    color: "#F6F8FC"
    Shortcut {{ sequence: "Escape"; onActivated: Qt.quit() }}
    Column {{
        id: errorContents
        anchors.fill: parent; anchors.margins: 28; spacing: 18
        Row {{
            width: parent.width; spacing: 8
            Label {{ width: parent.width - 52; height: 44; verticalAlignment: Text.AlignVCenter; text: "Email Reader needs your attention"; font.pixelSize: 20; font.bold: true; color: "#202124" }}
            Button {{
                objectName: "closeStartupError"; width: 44; height: 44
                text: "×"; Accessible.name: "Close startup error"
                onClicked: Qt.quit()
                contentItem: Text {{ text: parent.text; color: "#202124"; font.pixelSize: 22; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }}
                background: Rectangle {{ color: parent.down ? "#D7E6FD" : "white"; radius: 8; border.color: "#80868B" }}
            }}
        }}
        Label {{ width: parent.width; wrapMode: Text.Wrap; text: {json.dumps(startup_error_message(error))}; color: "#3C4043" }}
        Button {{ text: "Close"; onClicked: Qt.quit() }}
    }}
}}""".encode()


def dispose_qml_engine(engine) -> None:
    """Finish image-reader cleanup before Python's interpreter finalization.

    An application-parented engine otherwise survives main() and is destroyed by
    PySide's atexit handler. The live shutdown stack showed that handler waiting
    indefinitely for QQuickPixmapReader. Keep cleanup within the live interpreter.
    """
    import shiboken6

    if shiboken6.isValid(engine):
        shiboken6.delete(engine)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--extract-worker"]:
        from email_reader.extractor_worker import worker_main
        return worker_main()
    if arguments == ["--check-process-launch"]:
        from email_reader.process_diagnostics import run_probe
        return run_probe()
    from email_reader.browser_launch import register_browser_opener
    register_browser_opener()
    startup_started = time.perf_counter()
    args = build_parser().parse_args(argv)
    startup_parts = {}
    # App bundles are replaced in place during signed local updates. Disable
    # the per-user compiled QML cache so a new bundle can never display stale
    # controls or an older renderer from the same absolute path.
    os.environ.setdefault("QML_DISABLE_DISK_CACHE", "1")
    if args.check_provider_setup:
        if args.demo:
            print("Provider setup check requires the signed production app", file=sys.stderr)
            return 2
        from email_reader.security import KeychainVault

        try:
            google_ready, microsoft_ready = provider_oauth_client_readiness(KeychainVault())
        except Exception:
            print("Provider setup could not be checked; no account was changed", file=sys.stderr)
            return 2
        print(f"Google OAuth client configured: {'yes' if google_ready else 'no'}")
        print(f"Microsoft OAuth client configured: {'yes' if microsoft_ready else 'no'}")
        print("No sign-in, account change, mail read, draft save, or send was attempted")
        return 0
    if args.demo:
        os.environ["EMAIL_READER_TESTING"] = "1"
        demo_path = Path.cwd() / "data" / "demo"
        os.environ["EMAIL_READER_DATA_DIR"] = str(demo_path)

    from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, QUrl, Slot
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtWebEngineQuick import QtWebEngineQuick

    from email_reader.context import ApplicationContext
    from email_reader.controller import AppController
    from email_reader.demo import populate_demo
    startup_parts["imports"] = time.perf_counter() - startup_started

    QCoreApplication.setOrganizationName("Private Email Reader")
    QCoreApplication.setApplicationName("Email Reader")
    QQuickStyle.setStyle("Basic")
    QtWebEngineQuick.initialize()
    app = QGuiApplication(sys.argv[:1])
    app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    context_started = time.perf_counter()
    try:
        context = ApplicationContext.create(testing=args.demo)
        startup_parts["context"] = time.perf_counter() - context_started
    except Exception as exc:
        sys.stderr.write(startup_error_message(str(exc)) + "\n")
        if args.check_storage or args.sync_health or args.inspect_import or args.recover_import:
            return 2
        if args.check_access or args.sync_once or args.audit_sources or args.audit_search or args.audit_search_suite or args.search_suite_report or args.audit_local_retrieval or args.reindex_local_pdfs or args.reindex_local_text or args.reindex_local_office or args.benchmark_read_open or args.freeze_rendering_reference_set or args.reply_cohort_availability or args.audit_observed_snapshot_case or args.benchmark_replies or args.review_reply_benchmarks or args.review_reply_case or args.review_reply_cohort_case or args.review_search_case or args.review_rendering_gaps:
            return 2
        error_engine = QQmlApplicationEngine()
        error_engine.loadData(startup_error_qml(str(exc)))
        return app.exec() if error_engine.rootObjects() else 2
    if args.check_access:
        print("Private data unlocked successfully")
        context.close()
        return 0
    if args.check_storage:
        try:
            valid = context.database.integrity_check()
            print("Encrypted database integrity: " + ("ok" if valid else "failed"))
            return 0 if valid else 2
        finally:
            context.close()
    if args.sync_health or args.inspect_import or args.recover_import:
        from email_reader.diagnostics import inspect_import, sync_health
        try:
            print(json.dumps(inspect_import(
                context, args.inspect_import or args.recover_import,
                recover=bool(args.recover_import), account_number=args.import_account_number,
            ) if args.inspect_import or args.recover_import else sync_health(context), indent=2))
        finally:
            context.close()
        return 0
    if args.review_reply_case or args.review_reply_cohort_case or args.review_search_case or args.review_rendering_gaps:
        from email_reader.diagnostics import reply_case_review_text, reply_cohort_case_text

        try:
            if args.annotate_observed_intent and not (
                args.review_reply_cohort_case and args.benchmark_cohort == "observed"
            ):
                raise ValueError("Private intent notes require an observed-cohort case")
            if args.review_rendering_gaps:
                from email_reader.diagnostics import rendering_gap_review_text
                review_text = rendering_gap_review_text(context.database)
            elif args.review_search_case:
                from email_reader.search_audit import search_case_review_text
                review_text = search_case_review_text(context, args.review_search_case)
            elif args.review_reply_cohort_case:
                review_text = reply_cohort_case_text(
                    context, args.benchmark_cohort, args.review_reply_cohort_case
                )
            else:
                if not args.review_reply_benchmarks:
                    raise ValueError("Two benchmark labels are required")
                review_text = reply_case_review_text(
                    context, *args.review_reply_benchmarks, args.review_reply_case
                )
            intent_text = ""
            if args.annotate_observed_intent:
                from email_reader.diagnostics import observed_reply_intent
                intent_text = observed_reply_intent(context, args.review_reply_cohort_case)
        except Exception as exc:
            print(
                f"Private review unavailable ({type(exc).__name__}); no content logged",
                file=sys.stderr,
            )
            context.close()
            return 2
        class IntentBridge(QObject):
            @Slot(str, result=bool)
            def save(self, value: str) -> bool:
                if not args.annotate_observed_intent:
                    return False
                try:
                    from email_reader.diagnostics import save_observed_reply_intent
                    save_observed_reply_intent(
                        context, args.review_reply_cohort_case, value
                    )
                    return True
                except Exception:
                    # Neither private instructions nor storage details enter logs.
                    return False

        intent_bridge = IntentBridge(app)
        review_engine = QQmlApplicationEngine(app)
        review_engine.rootContext().setContextProperty("ReviewText", review_text)
        review_engine.rootContext().setContextProperty("ReviewIntentEnabled", args.annotate_observed_intent)
        review_engine.rootContext().setContextProperty("ReviewIntent", intent_text)
        review_engine.rootContext().setContextProperty("ReviewIntentBridge", intent_bridge)
        review_engine.loadData(b"""import QtQuick
import QtQuick.Controls
ApplicationWindow {
    visible: true
    width: 1020; height: 760
    title: "Private email review"
    color: "#F6F8FC"
    Column {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 10
        Row {
            width: parent.width
            Label { text: "Read-only email review"; font.pixelSize: 20; color: "#202124" }
            Item { width: parent.width - 260; height: 1 }
            Button { text: "Close"; onClicked: Qt.quit() }
        }
        ScrollView {
            width: parent.width
            height: parent.height - (ReviewIntentEnabled ? 245 : 58)
            TextArea {
                text: ReviewText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: "#202124"
                font.pixelSize: 15
            }
        }
        Column {
            visible: ReviewIntentEnabled
            width: parent.width
            spacing: 6
            Label {
                text: "Your intended stance and any facts only you know (never copy the held-out reply)"
                color: "#202124"; wrapMode: Text.Wrap; width: parent.width
            }
            ScrollView {
                width: parent.width; height: 92
                TextArea {
                    id: intentInput
                    text: ReviewIntent
                    wrapMode: TextEdit.Wrap
                    color: "#202124"
                    font.pixelSize: 15
                    placeholderText: "Example: decline politely because I am unavailable that day."
                }
            }
            Row {
                spacing: 12
                Button {
                    text: "Save private benchmark intent"
                    onClicked: intentStatus.text = ReviewIntentBridge.save(intentInput.text)
                        ? "Saved encrypted note" : "Could not save; write 1-1200 characters"
                }
                Label { id: intentStatus; color: "#3C4043" }
            }
        }
    }
}""")
        if not review_engine.rootObjects():
            dispose_qml_engine(review_engine)
            context.close()
            return 3
        try:
            return app.exec()
        finally:
            dispose_qml_engine(review_engine)
            context.close()
    if args.audit_sources or args.audit_search or args.audit_search_suite or args.search_suite_report or args.audit_local_retrieval or args.reindex_local_pdfs or args.reindex_local_text or args.reindex_local_office or args.benchmark_read_open or args.freeze_rendering_reference_set or args.reply_cohort_availability or args.audit_observed_snapshot_case or args.benchmark_replies or args.review_reply_benchmarks:
        from email_reader.diagnostics import (
            audit_observed_reply_snapshot,
            audit_search,
            audit_sources,
            benchmark_read_open,
            benchmark_replies,
            freeze_rendering_reference_set,
            reply_cohort_availability,
            review_reply_benchmark_pair,
        )

        try:
            if args.reindex_local_pdfs:
                result = context.repository.reindex_outdated_pdf_extractions(args.reindex_limit)
                print(json.dumps({"local_only": True, "provider_reads": 0, **result}))
            elif args.reindex_local_text:
                result = context.repository.reindex_outdated_text_extractions(args.reindex_limit)
                print(json.dumps({"local_only": True, "provider_reads": 0, **result}))
            elif args.reindex_local_office:
                result = context.repository.reindex_outdated_office_extractions(args.reindex_limit)
                print(json.dumps({"local_only": True, "provider_reads": 0, **result}))
            elif args.audit_local_retrieval:
                from email_reader.search_audit import audit_local_retrieval
                print(json.dumps(audit_local_retrieval(context), indent=2))
            elif args.audit_sources:
                print(json.dumps(audit_sources(context), indent=2))
            elif args.audit_search:
                print(json.dumps(audit_search(context), indent=2))
            elif args.audit_search_suite:
                from email_reader.search_audit import audit_search_suite
                print(json.dumps(audit_search_suite(context), indent=2))
            elif args.search_suite_report:
                from email_reader.search_audit import search_suite_report
                print(json.dumps(search_suite_report(context), indent=2))
            elif args.benchmark_read_open:
                print(json.dumps(benchmark_read_open(context), sort_keys=True))
            elif args.freeze_rendering_reference_set:
                print(json.dumps(freeze_rendering_reference_set(context), sort_keys=True))
            elif args.reply_cohort_availability:
                print(json.dumps(reply_cohort_availability(context), sort_keys=True))
            elif args.audit_observed_snapshot_case:
                print(json.dumps(audit_observed_reply_snapshot(context, args.audit_observed_snapshot_case), sort_keys=True))
            elif args.review_reply_benchmarks:
                print(json.dumps(review_reply_benchmark_pair(context, *args.review_reply_benchmarks), indent=2))
            else:
                benchmark_replies(
                    context, args.benchmark_replies, args.benchmark_label,
                    args.benchmark_effort, args.benchmark_compact, args.benchmark_cohort,
                    args.benchmark_require_observed_intents,
                )
            return 0
        except Exception as exc:
            print(
                f"Diagnostic stopped safely ({type(exc).__name__}); private details were not logged",
                file=sys.stderr,
            )
            return 2
        finally:
            context.close()
    if args.demo:
        populate_demo(context.repository)
    controller_started = time.perf_counter()
    controller = AppController(context)
    startup_parts["controller"] = time.perf_counter() - controller_started
    controller.setParent(app)
    def close_context():
        controller.shutdown()
        context.close()
    app.aboutToQuit.connect(close_context)

    if args.sync_once:
        run_started = False
        exit_scheduled = False
        sync_started = 0.0
        print(f"Startup: {time.perf_counter() - startup_started:.3f}s", flush=True)
        print("Startup components: " + json.dumps(startup_parts), flush=True)

        def finish_sync() -> None:
            nonlocal exit_scheduled
            if exit_scheduled or controller.busy:
                return
            exit_scheduled = True
            phase = str(controller.activity.get("phase") or "")
            print(f"Refresh: {time.perf_counter() - sync_started:.3f}s", flush=True)
            print("Refresh components: " + json.dumps(controller.sync_timings), flush=True)
            sys.stdout.write(f"{controller.status}\n")
            app.exit(0 if phase == "complete" else 1)

        def busy_changed() -> None:
            nonlocal run_started
            if controller.busy:
                run_started = True
            elif run_started:
                # Let the controller finish setting its status/activity after
                # busyChanged is emitted.
                QTimer.singleShot(0, finish_sync)

        def begin_sync() -> None:
            nonlocal sync_started
            sync_started = time.perf_counter()
            controller.busyChanged.connect(busy_changed)
            controller.syncAll()
            if not controller.busy:
                QTimer.singleShot(0, finish_sync)

        def timed_out() -> None:
            nonlocal exit_scheduled
            if exit_scheduled:
                return
            exit_scheduled = True
            sys.stderr.write("Email refresh did not finish within 10 minutes\n")
            app.exit(4)

        QTimer.singleShot(0, begin_sync)
        QTimer.singleShot(600_000, timed_out)
        return app.exec()

    engine = QQmlApplicationEngine(app)
    engine.addImageProvider("mail", controller.mail_images)
    engine.rootContext().setContextProperty("App", controller)
    engine.load(QUrl.fromLocalFile(str(qml_entrypoint())))
    if not engine.rootObjects():
        dispose_qml_engine(engine)
        context.close()
        return 3
    if not args.demo:
        QTimer.singleShot(1500, controller._automatic_sync)
        app.applicationStateChanged.connect(
            lambda state: controller._automatic_sync() if state == Qt.ApplicationActive else None
        )
    try:
        return app.exec()
    finally:
        dispose_qml_engine(engine)


if __name__ == "__main__":
    raise SystemExit(main())
