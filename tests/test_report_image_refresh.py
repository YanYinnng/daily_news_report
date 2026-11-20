import contextlib
import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src.config import DEFAULT_SETTINGS
from src.image_selection import VERSION
from src.models import ArticleContent, ArticleDigest, ArticleMeta
from src.report import build_html, build_markdown
from src.report_image_refresh import parse_articles, refresh_images


class ReportImageRefreshTests(unittest.TestCase):
    def fixture(self, root, with_candidates=True):
        day=root/'reports/calendar/2026-10-15';day.mkdir(parents=True)
        urls=[f'https://mmbiz.qpic.cn/test{i}/640' for i in range(4)]
        meta=ArticleMeta('A','a','Existing title','https://weread.qq.com/web/mp/content?reviewId=MP_WXS_123_a')
        digest=ArticleDigest(meta,ArticleContent(meta.url,image_urls=urls),'Original summary: 10 and 20. **Keep formatting.**')
        old_settings=dict(include_images='auto',max_images_per_article=2,max_total_images_per_theme=20)
        args=(date(2026,10,15),datetime(2026,10,15),datetime(2026,10,16),'Existing overview',[digest],[],old_settings)
        (day/'ai.md').write_text(build_markdown(*args),encoding='utf-8')
        (day/'ai.html').write_text(build_html(*args),encoding='utf-8')
        audit={'theme':'ai','status':'complete','generated_at':'original-generation-time',
               'articles':[{'account':'A','title':meta.title,'url':meta.url}], 'issues':[]}
        if with_candidates:
            audit['image_selection']=[{'article_url':meta.url,'candidates':[{'url':u} for u in urls]}]
        (day/'ai.audit.json').write_text(json.dumps(audit),encoding='utf-8')
        cache=root/'data/image_selection';cache.mkdir(parents=True)
        for index,url in enumerate(urls):
            folder=cache/hashlib.sha256(url.encode()).hexdigest();folder.mkdir()
            facts={'version':VERSION,'width':800,'height':600,'frames':1,'sharpness':100,'contrast':65,
                   'edge_density':.1,'horizontal_lines':0,'vertical_lines':0,'grid_intersections':0,
                   'long_lines':4,'shape_boxes':0,'qr_detected':False,'qr_coverage':0,
                   'perceptual_hash':str(index),'ocr_status':'ok','ocr_text':f'准确率对比 10% 20% 30% 40% 50% 60% model {index}'}
            (folder/'analysis.json').write_text(json.dumps(facts),encoding='utf-8')
        with contextlib.closing(sqlite3.connect(root/'data/calendar_state.sqlite3')) as db:
            db.execute('create table theme_reports (schedule_date text,theme_id text,status text,email_sent_at text)')
            db.execute("insert into theme_reports values ('2026-10-15','ai','success','already-sent')")
            db.commit()
        settings=copy.deepcopy(DEFAULT_SETTINGS);settings['image_selection']['cache_dir']=str(cache)
        return settings,day

    def test_refresh_changes_only_images_and_preserves_delivery_without_live_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings,day=self.fixture(root)
            old=json.loads((day/'ai.audit.json').read_text(encoding='utf-8'))
            with patch('src.image_selection.requests.get',side_effect=AssertionError('No network')) as get, \
                 patch('src.sources.weread.WeReadSource',side_effect=AssertionError('No live article reads')) as source, \
                 contextlib.redirect_stdout(io.StringIO()):
                result=refresh_images(root,settings,'2026-10-15')
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['themes'][0]['images'],3)
            self.assertTrue(result['summaries_unchanged']);self.assertTrue(result['delivery_state_unchanged'])
            self.assertEqual(result['model_calls'],0);self.assertEqual(result['emails_sent'],0)
            get.assert_not_called();source.assert_not_called()
            self.assertIn('Original summary: 10 and 20. **Keep formatting.**',(day/'ai.md').read_text(encoding='utf-8'))
            self.assertTrue((Path(result['backup_dir'])/'ai.html').exists())
            audit=json.loads((day/'ai.audit.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['generated_at'],old['generated_at']);self.assertEqual(audit['status'],'complete')

    def test_cached_only_reports_missing_candidates_and_keeps_previous_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings,day=self.fixture(root,with_candidates=False)
            with patch('src.sources.weread.WeReadSource',side_effect=AssertionError('No live request')) as source, contextlib.redirect_stdout(io.StringIO()):
                result=refresh_images(root,settings,'2026-10-15',cached_only=True)
            self.assertEqual(result['status'],'partial');self.assertEqual(len(result['source_errors']),1)
            self.assertEqual(result['themes'][0]['images'],2)
            source.assert_not_called()

    def test_intervening_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings,day=self.fixture(root)
            def intervene(*args):
                (day/'ai.md').write_text('User edited this file',encoding='utf-8')
            with patch('src.report_image_refresh.select_article_images',side_effect=intervene), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'changed while images'):
                    refresh_images(root,settings,'2026-10-15')
            self.assertEqual((day/'ai.md').read_text(),'User edited this file')

    def test_mismatched_audit_refuses_to_rewrite(self):
        with self.assertRaises(ValueError):
            parse_articles('#### 1. Title\n\n- 链接：https://example.com/wrong\n\nSummary\n',
                           {'articles':[{'url':'https://example.com/right','title':'Title','account':'A'}]})
