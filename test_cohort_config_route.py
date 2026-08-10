import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin_server


class CohortConfigRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = str(Path(self.temp_dir.name) / "cohort_config.json")
        self.base_config = {
            "cohortLabel": "11기",
            "applicationStartDate": "2026-08-01",
            "applicationEndDate": "2026-08-31",
            "todayOverride": None,
            "otDate": "2026-08-02",
            "commonSchedule": [],
            "trackSchedules": [
                {
                    "trackId": "builder",
                    "trackName": "빌더 트랙",
                    "weekday": "THU",
                    "sessionTime": "20:00",
                    "firstSessionDate": None,
                    "sessionCount": 4,
                    "announcementDaysBefore": 1,
                    "announcementTime": "18:00",
                    "exceptions": [],
                }
            ],
            "announcementScheduleEnabled": True,
        }
        self.patchers = [
            mock.patch.object(admin_server, "COHORT_CONFIG_FILE", self.config_path),
            mock.patch.object(
                admin_server, "_get_authenticated_discord_user", return_value={"id": "admin"}
            ),
            mock.patch.object(admin_server, "_is_admin_user", return_value=True),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertTrue(admin_server._write_cohort_config(copy.deepcopy(self.base_config)))
        self.client = admin_server.app.test_client()

    def test_unauthorized_put_is_rejected(self):
        with mock.patch.object(admin_server, "_get_authenticated_discord_user", return_value=None):
            response = self.client.put("/api/cohort-config", json={"applicationEndDate": "2026-09-01"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(admin_server._read_cohort_config()["applicationEndDate"], "2026-08-31")

    def test_partial_put_preserves_schedule_fields_and_get_returns_them(self):
        response = self.client.put(
            "/api/cohort-config", json={"applicationEndDate": "2026-09-01"}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        saved = response.get_json()
        self.assertEqual(saved["trackSchedules"], self.base_config["trackSchedules"])
        self.assertTrue(saved["announcementScheduleEnabled"])

        public = self.client.get("/api/cohort-config")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.get_json()["trackSchedules"], self.base_config["trackSchedules"])

    def test_invalid_schedule_does_not_mutate_persisted_config(self):
        invalid = copy.deepcopy(self.base_config["trackSchedules"])
        invalid[0]["trackId"] = "typo-track"
        response = self.client.put("/api/cohort-config", json={"trackSchedules": invalid})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            admin_server._read_cohort_config()["trackSchedules"],
            self.base_config["trackSchedules"],
        )

    def test_enabled_empty_schedule_is_rejected(self):
        response = self.client.put(
            "/api/cohort-config",
            json={"trackSchedules": [], "announcementScheduleEnabled": True},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            admin_server._read_cohort_config()["trackSchedules"],
            self.base_config["trackSchedules"],
        )


if __name__ == "__main__":
    unittest.main()
