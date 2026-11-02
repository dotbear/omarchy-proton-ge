import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proton_ge as ge


CONFIG = '''"InstallConfigStore" {
 "Software" { "Valve" { "Steam" {
  "Unrelated" "preserve me"
  "CompatToolMapping" {
   "0" { "name" "GE-Proton10-28" "config" "" "priority" "75" }
   // game settings must survive
   "123" { "name" "GE-Proton10-29" "config" "keep this" "priority" "250" }
   "456" { "name" "Proton-GE Latest" "config" "" "priority" "250" }
  }
 } } }
}
'''
MANIFEST = '''"compatibilitytools" { "compat_tools" {
 "GE-Proton11-12-x86_64" { "install_path" "." "display_name" "GE-Proton11-12-x86_64" "from_oslist" "windows" "to_oslist" "linux" }
} }
'''


def archive_bytes(tag="GE-Proton11-12", malicious=None):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as tar:
        for name, data in {"proton": b"a prebuilt runner", "version": ("123 " + tag).encode(), "compatibilitytool.vdf": MANIFEST.encode()}.items():
            member = tarfile.TarInfo(tag + "-x86_64/" + name)
            member.size = len(data)
            member.mode = 0o755 if name == "proton" else 0o644
            tar.addfile(member, io.BytesIO(data))
        if malicious:
            tar.addfile(malicious, io.BytesIO(b"x") if malicious.size else None)
    return result.getvalue()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.steam = self.root / "Steam"
        (self.steam / "config").mkdir(parents=True)
        (self.steam / "config/config.vdf").write_text(CONFIG)
        self.updater = ge.Updater(home=self.root, state_dir=self.root / "state", steam=self.steam)
        self.make_install(self.updater.current, "GE-Proton11-11")
        self.processes = patch.object(ge, "processes", return_value=(False, False))
        self.processes.start()
        self.addCleanup(self.processes.stop)
        self.addCleanup(self.temp.cleanup)
        self.payload = archive_bytes()
        self.release = {
            "tag": "GE-Proton11-12", "id": "123", "size": len(self.payload),
            "digest": hashlib.sha256(self.payload).hexdigest(),
            "url": ge.ASSETS + "GE-Proton11-12/GE-Proton11-12-x86_64.tar.gz",
        }

    def make_install(self, path, tag):
        path.mkdir(parents=True, exist_ok=True)
        (path / ".protonplus").write_text(json.dumps({"tag": tag}))
        (path / "version").write_text("123 " + tag)
        (path / "compatibilitytool.vdf").write_text(ge.manifest_for_latest(MANIFEST))
        (path / "proton").write_text("runner")

    def install(self):
        with patch.object(ge, "request", return_value=io.BytesIO(self.payload)):
            self.updater.install(self.release)

    def test_migration_changes_default_and_all_pins_preserves_unrelated_data(self):
        changed, count = ge.steam_selections(CONFIG)
        self.assertEqual(count, 2)
        self.assertNotIn("GE-Proton10-", changed)
        self.assertIn('"Unrelated" "preserve me"', changed)
        self.assertIn('"config" "keep this"', changed)
        self.assertIn("// game settings must survive", changed)
        self.assertEqual(ge.steam_selections(changed), (changed, 0))

    def test_migration_creates_missing_default_and_section(self):
        for config in ('"InstallConfigStore" { "Software" { "Valve" { "Steam" {} } } }', CONFIG.replace('"0" { "name" "GE-Proton10-28" "config" "" "priority" "75" }', '')):
            changed, count = ge.steam_selections(config)
            self.assertGreater(count, 0)
            self.assertIn('"0"', changed)
            self.assertEqual(ge.steam_selections(changed)[1], 0)

    def test_migration_waits_until_steam_exits_then_backs_up(self):
        with patch.object(ge, "processes", return_value=(True, True)):
            self.updater.sync_selections()
        self.assertEqual((self.steam / "config/config.vdf").read_text(), CONFIG)
        self.assertTrue(self.updater.state["selections_pending"])
        self.updater.sync_selections()
        self.assertTrue(self.updater.state["selections_ready"])
        self.assertEqual((self.root / "state/steam-config.before-proton-ge.vdf").read_text(), CONFIG)

    def test_malformed_config_is_not_modified(self):
        path = self.steam / "config/config.vdf"
        path.write_text(CONFIG[:-3])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.updater.sync_selections()
        self.assertEqual(path.read_bytes(), before)

    def test_install_preserves_identity_settings_and_previous_version(self):
        (self.updater.current / "user_settings.py").write_text("my settings")
        self.install()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-12")
        self.assertEqual(ge.version(self.updater.previous), "GE-Proton11-11")
        self.assertEqual((self.updater.current / "compatibilitytool.vdf").read_text().count('"Proton-GE Latest"'), 2)
        self.assertEqual((self.updater.current / "user_settings.py").read_text(), "my settings")
        self.assertFalse(list(self.updater.storage.glob("stage-*")))
        self.assertFalse((self.updater.storage / "transaction.json").exists())

    def test_rollback_restores_previous_and_pauses_automatic_updates(self):
        self.install()
        self.updater.rollback()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")
        self.assertEqual(ge.version(self.updater.previous), "GE-Proton11-12")
        self.assertFalse(self.updater.state["auto"])

    def test_running_game_defers_install_and_blocks_rollback(self):
        with patch.object(ge, "processes", return_value=(True, True)), patch.object(ge, "request") as request:
            self.updater.install(self.release)
            request.assert_not_called()
            with self.assertRaises(ValueError):
                self.updater.rollback()
        self.assertEqual(self.updater.state["phase"], "deferred")
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")

    def test_game_starting_during_download_defers_promotion(self):
        with patch.object(ge, "processes", side_effect=[(True, False), (True, True)]):
            self.install()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")
        self.assertEqual(self.updater.state["phase"], "deferred")

    def test_checksum_failure_leaves_current_and_previous_untouched(self):
        self.make_install(self.updater.previous, "GE-Proton11-10")
        self.release["digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.install()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")
        self.assertEqual(ge.version(self.updater.previous), "GE-Proton11-10")

    def test_install_of_current_version_never_downloads(self):
        self.release["tag"] = "GE-Proton11-11"
        self.release["url"] = ge.ASSETS + "GE-Proton11-11/GE-Proton11-11-x86_64.tar.gz"
        with patch.object(ge, "request") as request:
            self.updater.install(self.release)
            request.assert_not_called()

    def test_download_cannot_exceed_release_size(self):
        self.release["size"] = 1
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.install()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")

    def test_tar_traversal_and_escaping_symlink_are_rejected(self):
        for kind in ("traversal", "symlink"):
            malicious = tarfile.TarInfo("../../escape" if kind == "traversal" else "GE-Proton11-12-x86_64/link")
            if kind == "traversal":
                malicious.size = 1
            else:
                malicious.type = tarfile.SYMTYPE
                malicious.linkname = "../../outside"
            path = self.root / (kind + ".tar.gz")
            path.write_bytes(archive_bytes(malicious=malicious))
            with self.assertRaises((ValueError, tarfile.FilterError)):
                ge.extract(path, self.root / kind)
        self.assertFalse((self.root / "escape").exists())

    def test_tar_expansion_and_collection_limits(self):
        path = self.root / "archive.tar.gz"
        path.write_bytes(self.payload)
        for option, value in (("MAX_EXPANDED", 1), ("MAX_MEMBERS", 1)):
            with patch.object(ge, option, value), self.assertRaisesRegex(ValueError, "limit"):
                ge.extract(path, self.root / option)

    def test_wrong_archive_version_never_replaces_current(self):
        self.payload = archive_bytes(tag="GE-Proton11-10")
        self.release.update(size=len(self.payload), digest=hashlib.sha256(self.payload).hexdigest())
        with self.assertRaisesRegex(ValueError, "version"):
            self.install()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")

    def test_recovery_before_and_after_atomic_exchange(self):
        self.updater.storage.mkdir(exist_ok=True)
        candidate = self.updater.storage / "candidate"
        transaction = self.updater.storage / "transaction.json"
        self.make_install(candidate, "GE-Proton11-12")
        transaction.write_text(json.dumps({"tag": "GE-Proton11-12"}))
        self.updater.recover()
        self.assertFalse(candidate.exists())
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-11")
        self.make_install(candidate, "GE-Proton11-12")
        transaction.write_text(json.dumps({"tag": "GE-Proton11-12"}))
        ge.exchange(self.updater.current, candidate)
        self.updater.recover()
        self.assertEqual(ge.version(self.updater.current), "GE-Proton11-12")
        self.assertEqual(ge.version(self.updater.previous), "GE-Proton11-11")

    def test_tick_checks_hourly_and_pause_disables_network(self):
        self.updater.state.update(checked_at=ge.time.time(), phase="current")
        with patch.object(ge, "latest_release") as latest:
            self.updater.run("tick")
            latest.assert_not_called()
            self.updater.run("pause")
            self.updater.state["checked_at"] = 0
            self.updater.run("tick")
            latest.assert_not_called()

    def test_markup_and_foreign_urls_are_rejected_and_display_text_is_plain(self):
        for updates in ({"tag": '<img src="https://example.invalid/tracker">'}, {"url": "https://example.invalid/archive.tar.gz"}, {"digest": "x" * 64}):
            with self.assertRaises(ValueError):
                ge.validate_release(dict(self.release, **updates))
        (self.updater.current / ".protonplus").write_text(json.dumps({"tag": '<img src="https://example.invalid/tracker">'}))
        with self.assertRaises(ValueError):
            ge.version(self.updater.current)
        qml = (Path(__file__).resolve().parents[1] / "BarWidget.qml").read_text()
        # All plugin text uses this local PlainText type. The one shared
        # tooltip sink receives only the plugin's fixed translated label.
        self.assertEqual(qml.count(": Text {"), 1)
        self.assertIn("textFormat: Text.PlainText", qml)
        self.assertIn('tooltipText: strings.t("tooltip")', qml)
        self.assertNotIn("tooltipText: root.state", qml)


if __name__ == "__main__":
    unittest.main()
