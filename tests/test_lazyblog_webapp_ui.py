"""Small DOM-contract and real-JavaScript race regression tests (no model calls)."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lazyblog_webapp as web


class StudioUITests(unittest.TestCase):
    def test_unique_ids_and_complete_script_bindings(self):
        class IDs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids = []

            def handle_starttag(self, tag, attrs):
                self.ids.extend(value for key, value in attrs if key == "id")

        parser = IDs()
        parser.feed(web.INDEX_HTML)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for name in re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', web.INDEX_HTML):
            self.assertIn(name, parser.ids)
        self.assertNotIn("fonts.googleapis.com", web.INDEX_HTML + web.LOGIN_HTML + web.STUDIO_THEME_CSS)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for JS regression checks")
    def test_inline_scripts_parse(self):
        for source in (web.INDEX_HTML, web.LOGIN_HTML):
            scripts = re.findall(r"<script>(.*?)</script>", source, re.S)
            self.assertTrue(scripts)
            subprocess.run(["node", "--check", "-"], input="\n".join(scripts), text=True, check=True, capture_output=True)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for JS regression checks")
    def test_late_history_response_cannot_replace_new_conversation(self):
        def function(name, next_name):
            start = web.INDEX_HTML.index(f"    async function {name}(")
            end = web.INDEX_HTML.index(f"    async function {next_name}(", start)
            return web.INDEX_HTML[start:end]

        script = """
const assert = require('node:assert/strict');
const state = {sessionViewRevision:0,sessionId:'old',composerDirty:false};
let resolveRequest;
const api = () => new Promise(resolve => {resolveRequest = resolve;});
const stopSpeechRecognition = () => {};
const renderSession = () => {throw Error('stale selection rendered');};
const mergeSessionPayload = () => {throw Error('stale refresh rendered');};
const renderSessions = () => {};
""" + function("loadSessions", "loadJobs") + function("loadSession", "pollActiveSession")
        # pollActiveSession is followed by a synchronous helper.
        start = web.INDEX_HTML.index("    async function pollActiveSession(")
        end = web.INDEX_HTML.index("    function queueRealtimeRefresh(", start)
        script += web.INDEX_HTML[start:end]
        script += """
(async () => {
  let pending = loadSession('old');
  state.sessionViewRevision++; state.sessionId = null;
  resolveRequest({session:{id:'old'}}); await pending;
  state.sessionId = 'old';
  pending = pollActiveSession();
  state.sessionViewRevision++; state.sessionId = null;
  resolveRequest({session:{id:'old'}}); await pending;
  pending = loadSessions({autoload:true});
  state.sessionViewRevision++;
  resolveRequest({sessions:[{id:'old'}]}); await pending;
  assert.equal(state.sessionId,null);
  assert.equal(state.sessionPollInFlight,false);
})().catch(error => {console.error(error);process.exit(1);});
"""
        subprocess.run(["node", "-"], input=script, text=True, check=True, capture_output=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
