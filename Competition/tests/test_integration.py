import json
from pathlib import Path
import threading
import unittest
from urllib.request import Request, urlopen

from agent.policy import Agent
from agent.protocol import schema_errors, empty_response
from agent.rules import command, xy
from agent.server import make_server
from local_judge.engine import Game, ReplayServices


class IntegrationTests(unittest.TestCase):
    def test_sample_and_second_level_targets(self):
        root = Path(__file__).resolve().parents[1]
        request = json.loads((root / "docs" / "request.txt").read_text(encoding="utf-8"))
        output = Agent().decide(request)
        self.assertEqual(schema_errors(output), [])

    def test_http_utf8_duplicate_and_malformed_requests(self):
        server = make_server(0, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            body = json.dumps(Game(pressure=0).observation("challenger"), ensure_ascii=False).encode("utf-8")
            def post(raw):
                with urlopen(Request(url, raw, {"Content-Type": "application/json"}), timeout=5) as response:
                    return json.load(response)
            first = post(body)
            self.assertEqual(schema_errors(first), [])
            self.assertEqual(post(body), first)
            with self.assertLogs("agent.server", level="ERROR"):
                self.assertEqual(post(b'{broken'), empty_response())
            self.assertEqual(post(body), first)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_malformed_types_do_not_crash_judge(self):
        cases = ({"action": []}, {"action": "use", "name": {}}, {"action": "move", "targetPos": [None]},
                 {"action": "buy", "name": "Medicine", "num": True}, {"action": "attack", "controllerId": []})
        for cmd in cases:
            with self.subTest(cmd=cmd):
                game = Game(pressure=0)
                game.step({"challenger": {"roleCommandMap": {"10010": cmd}}})
                self.assertEqual(game.exceptions["challenger"], 1)

    def test_task_llm_command_result_answer_pipeline(self):
        game = Game(pressure=0)
        game.task_specs["challenger"][0][0]["description"] = "Use sandbox tool to answer the unknown task."
        game.task_specs["challenger"][0][0]["answer"] = {"answer": 42}
        game.unit(10011)["pos"] = xy((13, 14))
        game.step({"challenger": {"roleCommandMap": {"10011": command("acceptTask")}}})
        agent = Agent()
        first = agent.decide(game.observation("challenger"))
        self.assertTrue(first["prompt"])
        game.services.prompts[first["prompt"]] = json.dumps({"executeCmd": "read-fixture", "skill": "Read result field."})
        game.services.commands["read-fixture"] = '[exitCode:0]\n{"answer":42}'
        game.step({"challenger": first})
        second = agent.decide(game.observation("challenger"))
        self.assertEqual(second["executeCmd"], "read-fixture")
        game.step({"challenger": second})
        third = agent.decide(game.observation("challenger"))
        self.assertIn("answer", third["prompt"])
        self.assertIn("42", third["prompt"])
        game.services.prompts[third["prompt"]] = json.dumps({"answer": '{"answer":42}'})
        game.step({"challenger": third})
        fourth = agent.decide(game.observation("challenger"))
        self.assertEqual(fourth["roleCommandMap"]["10011"]["action"], "submitAnswer")
        game.step({"challenger": fourth})
        self.assertIsNone(game.task_state["challenger"])
        self.assertGreater(game.score["challenger"]["task"], 50)

    def test_local_sandbox_does_not_execute_and_truncates(self):
        services = ReplayServices(commands={"long": "[exitCode:0]\n" + "中" * 70000})
        self.assertTrue(services.command("echo never executed").startswith("[JUDGER_ERROR]"))
        result = services.command("long")
        self.assertTrue(result.endswith("[TRUNCATED]"))
        self.assertLessEqual(len(result.encode("utf-8")), 65536 + len("\n[TRUNCATED]"))


if __name__ == "__main__":
    unittest.main()
