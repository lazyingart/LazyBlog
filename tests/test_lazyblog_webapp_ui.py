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
    def test_compact_sync_status_keeps_errors_and_local_only_state(self):
        start = web.INDEX_HTML.index("    function setComposerStatus(")
        end = web.INDEX_HTML.index("    function resizeMobileComposer(", start)
        script = """
const assert = require('node:assert/strict');
const nodes = {
  composerStatus: {dataset:{}, setAttribute(key,value){this[key]=value;}},
  composerStatusDetail: {dataset:{}, hidden:true},
};
const $ = id => nodes[id];
const window = {matchMedia:()=>({matches:true})};
const resizeMobileComposer = () => {};
""" + web.INDEX_HTML[start:end] + """
assert.equal(compactComposerStatus('Saved to workspace','saved',true),'Saved');
for (const text of ['Saved on this device','Saved locally','Recovered draft']) {
  assert.equal(compactComposerStatus(text,'saved',true),'On device');
}
assert.equal(compactComposerStatus('Saving draft...','saving',true),'Saving…');
assert.equal(compactComposerStatus('Listening in English','',true),'Listening…');
assert.equal(compactComposerStatus('Network failed','error',true),'Check sync');
assert.equal(compactComposerStatus('Saved locally','saved',false),'Saved locally');
setComposerStatus('Network failed; draft saved on this device','error');
assert.equal(nodes.composerStatusDetail.hidden,false);
assert.equal(nodes.composerStatus['aria-expanded'],'true');
assert.equal(nodes.composerStatus.textContent,'Check sync');
assert.match(nodes.composerStatusDetail.textContent,/Network failed/);
assert.match(nodes.composerStatus['aria-label'],/Network failed/);
setComposerStatus('Saved to workspace','saved');
assert.equal(nodes.composerStatusDetail.hidden,true);
assert.equal(nodes.composerStatus['aria-expanded'],'false');
// A detail panel opened manually must stay open as status updates arrive.
nodes.composerStatusDetail.hidden=false;
nodes.composerStatusDetail.dataset.autoError='false';
setComposerStatus('Saving draft...','saving');
assert.equal(nodes.composerStatusDetail.hidden,false);
"""
        subprocess.run(["node", "-"], input=script, text=True, check=True, capture_output=True, timeout=5)

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
