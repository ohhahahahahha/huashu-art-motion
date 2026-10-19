"""Story-specific action playback and whiteboard reveal, using temporary geometric fixtures.

No image generation service or private character assets are used. Browser tests need
Playwright Chromium; schema/lint checks run with the standard library alone.
"""
import base64
import functools
import http.server
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'scripts' / 'engine'


class StoryLint(unittest.TestCase):
    def lint(self, spec):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shot.json'
            path.write_text(json.dumps(spec), encoding='utf-8')
            return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'clip_lint.py'), str(path)],
                                  capture_output=True, text=True)

    def test_character_needs_action_and_frames(self):
        result = self.lint({'grammar': 'y3_whiteboard', 'duration': 4, 'cues': [
            {'at': 0, 'kind': 'character', 'image': 'data:image/png;base64,x',
             'data': {'actor': 'lead', 'box': [0, 0, 1, 1]}}]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frames', result.stdout)
        self.assertIn('data.action', result.stdout)


class StoryPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PIL import Image, ImageDraw
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest('needs Pillow and Playwright Chromium')
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as error:
            cls.pw.stop()
            if 'Executable doesn' in str(error):
                raise unittest.SkipTest('needs Playwright Chromium') from error
            raise
        cls.images = []
        for color in ('#CC3333', '#339955', '#222222'):
            im = Image.new('RGBA', (80, 120))
            ImageDraw.Draw(im).rectangle((8, 12, 72, 108), fill=color)
            buf = io.BytesIO(); im.save(buf, format='PNG')
            cls.images.append('data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode())
        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(ENGINE)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def cue(self, **kwargs):
        return {'at': 0, 'kind': 'character', 'frames': self.images, 'fps': 2,
                'data': {'actor': 'lead', 'action': 'reaches out', 'box': [0.1, 0, 0.4, 1], 'reveal': 1}, **kwargs}

    def boot(self, cues, **kwargs):
        spec = {'grammar': 'y3_whiteboard', 'duration': 4, 'width': 400, 'height': 300,
                'safe': {'bottom': 60}, 'cues': cues, **kwargs}
        self.page.add_init_script('window.CLIP_SPEC=' + json.dumps(spec))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/clip.html?render=1')
        self.page.wait_for_function('window.__ready || window.__bootFailed')
        return self.page.evaluate('window.__bootFailed || null')

    def pixel(self, time, x, y):
        return self.page.evaluate('''([t,x,y]) => {
            window.renderFrame(t);
            return Array.from(window.__canvas.getContext('2d').getImageData(x,y,1,1).data);
        }''', [time, x, y])

    def test_reveal_stays_on_first_pose_then_plays_action(self):
        self.assertIsNone(self.boot([self.cue()]))
        self.assertEqual(self.pixel(0.15, 100, 205)[:3], [251, 251, 251])
        self.assertEqual(self.pixel(0.9, 100, 160)[:3], [204, 51, 51])
        self.assertEqual(self.pixel(1.1, 100, 100)[:3], [204, 51, 51])
        self.assertEqual(self.pixel(1.6, 100, 100)[:3], [51, 153, 85])
        self.assertEqual(self.pixel(3.8, 100, 100)[:3], [34, 34, 34])

    def test_movement_respects_safe_area_and_scrubbing_is_deterministic(self):
        cue = self.cue()
        cue['data']['to'] = [0.5, 0, 0.4, 1]
        self.assertIsNone(self.boot([cue]))
        self.assertEqual(self.pixel(3.8, 280, 100)[:3], [34, 34, 34])
        self.assertEqual(self.pixel(3.8, 100, 100)[:3], [251, 251, 251])
        self.assertEqual(self.pixel(3.8, 280, 270)[:3], [251, 251, 251])
        first = self.page.evaluate("window.__canvas.toDataURL()")
        self.pixel(0.2, 20, 20)
        self.pixel(3.8, 280, 270)
        self.assertEqual(first, self.page.evaluate("window.__canvas.toDataURL()"))

    def test_new_pose_replaces_previous_pose_without_drawing_again(self):
        first = self.cue()
        second = self.cue(at=2, frames=[self.images[1]])
        second['data'] = {**second['data'], 'reveal': 0}
        self.assertIsNone(self.boot([first, second]))
        self.assertEqual(self.pixel(2, 100, 100)[:3], [51, 153, 85])

    def test_two_actors_remain_visible_after_staggered_drawing(self):
        first = self.cue(frames=[self.images[0]])
        first['data']['reveal'] = 0.5
        second = self.cue(at=0.5, frames=[self.images[1]])
        second['data'] = {**second['data'], 'actor': 'partner', 'box': [0.55, 0, 0.4, 1], 'reveal': 0.5}
        self.assertIsNone(self.boot([first, second]))
        self.assertEqual(self.pixel(1.6, 100, 100)[:3], [204, 51, 51])
        self.assertEqual(self.pixel(1.6, 280, 100)[:3], [51, 153, 85])

    def test_transparent_output_has_no_board(self):
        self.assertIsNone(self.boot([self.cue()], alpha=True))
        self.assertEqual(self.pixel(1.6, 5, 270)[3], 0)
        self.assertEqual(self.pixel(1.6, 100, 100), [51, 153, 85, 255])

    def test_conflicting_reveals_are_rejected(self):
        first = self.cue()
        second = self.cue(at=0.2)
        second['data']['actor'] = 'partner'
        self.assertIn('同时绘制', self.boot([first, second]))

    def test_conflicting_actions_for_one_actor_are_rejected(self):
        self.assertIn('片段重叠', self.boot([self.cue(dur=3), self.cue(at=2)]))

    def test_missing_action_frames_are_rejected(self):
        cue = self.cue(); cue.pop('frames')
        cue['image'] = self.images[0]
        self.assertIn('本故事动作帧', self.boot([cue]))

    def test_invalid_box_is_rejected(self):
        cue = self.cue(); cue['data']['box'] = [0.8, 0, 0.4, 1]
        self.assertIn('safe 内容区', self.boot([cue]))

    def test_y4_explicit_missing_pose_cannot_fall_back_to_talk(self):
        self.assertIn('补生成', self.boot(
            [{'at': 0, 'kind': 'point', 'text': 'hello', 'data': {'pose': 'offer_umbrella'}}],
            grammar='y4_storytime', data={'character': {'frames': {'rest': self.images[0]}}}))

    def test_legacy_board_still_renders(self):
        self.assertIsNone(self.boot([{'at': 0, 'kind': 'draw', 'data': {'icon': 'clock'}}]))
        self.page.evaluate('window.renderFrame(1.5)')


if __name__ == '__main__':
    unittest.main()
