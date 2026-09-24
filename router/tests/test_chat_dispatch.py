import unittest

from chat_dispatch import ACK, first_http_url, followup_text, get_job, start_job, finish_job


class ChatDispatchTests(unittest.TestCase):
    def test_url_triggers_and_plain_chat_does_not(self):
        self.assertEqual(
            first_http_url("have a look at https://www.abc.net.au/ today"),
            "https://www.abc.net.au/",
        )
        self.assertIsNone(first_http_url("just chatting, no link"))
        self.assertIsNone(first_http_url("see file:///etc/passwd"))

    def test_followup_names_the_page_and_the_approval_link(self):
        text = followup_text(
            "https://www.abc.net.au/",
            {"found": "Top story", "pending_id": 4, "proposed_url": "http://127.0.0.1:8091/pages/index.php"},
        )
        self.assertIn("abc.net.au", text)
        self.assertIn("Top story", text)
        self.assertIn("#4", text)
        self.assertIn("pages/index.php", text)
        self.assertNotEqual(text, ACK)

    def test_job_starts_running_and_then_holds_the_followup(self):
        job_id = start_job("lan", "https://example.com", "look")
        self.assertEqual(get_job(job_id)["status"], "running")
        self.assertEqual(get_job(job_id)["ack"], ACK)
        finish_job(job_id, "done looking")
        self.assertEqual(get_job(job_id)["followup"], "done looking")
        self.assertEqual(get_job(job_id)["status"], "done")


if __name__ == "__main__":
    unittest.main()
