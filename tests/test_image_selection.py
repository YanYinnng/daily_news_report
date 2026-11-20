import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageFilter
import cv2

from src.config import DEFAULT_SETTINGS
from src.html_utils import extract_image_contexts
from src.image_selection import VERSION, classify_image, image_features, select_article_images
from src.models import ArticleContent, ArticleDigest, ArticleMeta
from src.processing import process_articles
from src.report import build_html, build_markdown, report_image_plan


def facts(**changes):
    result = dict(width=800, height=600, frames=1, sharpness=120, contrast=65, edge_density=.12,
                  horizontal_lines=0, vertical_lines=0, grid_intersections=0, qr_detected=False,
                  qr_coverage=0, perceptual_hash='1234')
    result.update(changes)
    return result


class ImageSelectionTests(unittest.TestCase):
    def test_real_qr_pixels_are_detected_without_decoding_or_following_payload(self):
        qr = cv2.QRCodeEncoder_create().encode('https://example.com/do-not-open')
        qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        qr = cv2.resize(qr, (400, 400), interpolation=cv2.INTER_NEAREST)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'qr.png'
            Image.fromarray(qr).save(path)
            feature = image_features(path)
        self.assertTrue(feature['qr_detected'])
        self.assertFalse(classify_image(feature, '', '', '技术进展')['keep'])

    def test_useful_recruitment_poster_survives_small_registration_qr(self):
        result = classify_image(facts(qr_detected=True, qr_coverage=.03),
                                '校园招聘岗位与报名安排。'*20, '', '2027届校园招聘')
        self.assertTrue(result['keep'])
        self.assertEqual(result['category'], 'body_poster')

    def test_marketing_card_is_rejected_but_chart_footer_does_not_erase_data(self):
        ad = classify_image(facts(), '扫码关注公众号，免费领取课程优惠券！'*6, '', 'AI研究进展')
        self.assertFalse(ad['keep'])
        self.assertEqual(ad['category'], 'promotion')
        chart = classify_image(facts(long_lines=4), '模型准确率对比：10% 20% 30% 40% 50% 60%。扫码关注。', '', '模型性能对比')
        self.assertTrue(chart['keep'])
        self.assertEqual(chart['category'], 'chart')

    def test_qr_footer_with_english_label_is_not_mistaken_for_body_text(self):
        text = 'Student Employment Services & Career Development Centre 关注我们获取就业信息'
        result = classify_image(facts(width=554,height=219,qr_detected=True,qr_coverage=.1), text, '', '校园招聘')
        self.assertEqual(result['category'], 'qr_code')
        self.assertFalse(result['keep'])

    def test_recommended_article_banner_is_not_promoted_by_numbers_and_body_context(self):
        row=classify_image(facts(width=1080,height=236),'量化和蒸馏让2B模型直逼7B教师，2026',
                           '模型对比与统计结果，准确率增长。更多阅读','投机解码模型提速')
        self.assertFalse(row['keep'])
        self.assertEqual(row['category'],'recommendation')

    def test_article_context_alone_cannot_label_an_image_a_chart(self):
        row=classify_image(facts(),'新作 2B 7B 2026','准确率增长对比图表','研究进展')
        self.assertNotEqual(row['category'],'chart')

    def test_decoration_and_visually_blurred_images_are_rejected(self):
        self.assertEqual(classify_image(facts(frames=60), '欢迎', '', '日报')['category'], 'decorative_gif')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'blur.png'
            picture = Image.new('L',(800,600),255)
            draw=ImageDraw.Draw(picture)
            for x in range(40,780,60): draw.rectangle((x,100,x+20,500),fill=20)
            picture.filter(ImageFilter.GaussianBlur(20)).save(path)
            feature=image_features(path)
        self.assertEqual(classify_image(feature,'','','技术文章')['category'], 'low_clarity')

    def test_table_requires_intersecting_grid_not_just_poster_border(self):
        text = '数据统计 10 20 30 40 50 60 招聘信息'*4
        poster = classify_image(facts(horizontal_lines=8,vertical_lines=8),text,'','校园招聘')
        self.assertNotEqual(poster['category'],'table')
        table = classify_image(facts(horizontal_lines=4,vertical_lines=3,grid_intersections=12),text,'','指标表格')
        self.assertEqual(table['category'],'table')

    def test_cached_analysis_selects_useful_later_image_without_network_or_ocr(self):
        urls=['https://mmbiz.qpic.cn/advert/640','https://mmbiz.qpic.cn/chart/640']
        with tempfile.TemporaryDirectory() as folder, patch('src.image_selection.requests.get') as get, patch('src.image_selection.recognize_image_files') as ocr:
            for index,url in enumerate(urls):
                directory=Path(folder)/hashlib.sha256(url.encode()).hexdigest();directory.mkdir()
                text='扫码关注公众号，免费领取课程优惠券！'*5 if index==0 else '准确率对比 10% 20% 30% 40% 50% 60%'
                (directory/'analysis.json').write_text(json.dumps(dict(version=VERSION,**facts(perceptual_hash=str(index),long_lines=4),ocr_status='ok',ocr_text=text)),encoding='utf-8')
            content=ArticleContent('article',image_urls=urls)
            select_article_images(ArticleMeta('A','a','模型准确率对比','article'),content,'',
                                  {'image_selection':{'enabled':True,'cache_dir':folder}})
            self.assertEqual(content.image_urls,[urls[1]])
            self.assertEqual(content.image_selection['examined'],2)
            json.dumps(content.image_selection)
            get.assert_not_called();ocr.assert_not_called()

    def test_untrusted_image_host_is_skipped_without_fetching(self):
        with tempfile.TemporaryDirectory() as folder, patch('src.image_selection.requests.get') as get:
            content=ArticleContent('article',image_urls=['https://127.0.0.1/a','https://example.com/a'])
            select_article_images(ArticleMeta('A','a','title','article'),content,'',{'image_selection':{'enabled':True,'cache_dir':folder}})
            self.assertEqual(content.image_urls,[])
            self.assertEqual(len(content.image_selection['candidates']),2)
            get.assert_not_called()

    def test_optional_analysis_failure_preserves_summary_and_is_auditable(self):
        settings=copy.deepcopy(DEFAULT_SETTINGS)
        source,summarizer=Mock(),Mock()
        source.fetch_article.return_value=ArticleContent('article',text='Useful facts.',image_urls=['https://mmbiz.qpic.cn/a'])
        summarizer.summarize_article.return_value='Valid summary'
        with patch('src.image_selection.select_article_images',side_effect=RuntimeError('offline')):
            issues=[]
            result=process_articles([ArticleMeta('A','a','title','article')],source,summarizer,settings,issues)[0]
        self.assertEqual(result.summary,'Valid summary')
        self.assertEqual(result.content.image_urls,[])
        self.assertIn('error',result.content.image_selection)
        self.assertEqual(issues,[])

    def test_caps_are_applied_after_quality_ranking_in_both_formats(self):
        def digest(title,scores):
            urls=[f'https://mmbiz.qpic.cn/{title}{i}' for i in range(len(scores))]
            content=ArticleContent('article',image_urls=urls)
            content.image_selection={'candidates':[dict(url=url,keep=True,score=score,position=i+1) for i,(url,score) in enumerate(zip(urls,scores))]}
            return ArticleDigest(ArticleMeta('A','a',title,'article'),content,'摘要')
        first,second=digest('first',[60,70,90]),digest('second',[95])
        settings={'include_images':'auto','max_images_per_article':2,'max_total_images_per_theme':2}
        plan=report_image_plan([first,second],settings)
        self.assertEqual(plan[id(first)],['https://mmbiz.qpic.cn/first2'])
        self.assertEqual(plan[id(second)],['https://mmbiz.qpic.cn/second0'])
        args=(date(2026,10,15),datetime(2026,10,15),datetime(2026,10,16),'总览',[first,second],[],settings)
        for report in (build_html(*args),build_markdown(*args)):
            self.assertIn('first2',report);self.assertIn('second0',report)
            self.assertNotIn('first0',report);self.assertNotIn('first1',report)

    def test_context_parser_preserves_captions_and_skips_script_images(self):
        rows=extract_image_contexts('<p>性能对比曲线</p><img data-src="https://mmbiz.qpic.cn/a"><p>模型准确率</p><script>evil</script>')
        self.assertEqual(len(rows),1)
        self.assertIn('性能对比',rows[0]['context'])
        self.assertIn('模型准确率',rows[0]['context'])

    def test_prose_about_architecture_is_not_itself_a_diagram(self):
        row=classify_image(facts(),'我们负责架构和流程设计，日常编写程序。'*20,'','工程师的一天')
        self.assertEqual(row['category'],'text_image')
        diagram=classify_image(facts(shape_boxes=4,long_lines=6),'模型框架与数据流程结构，输入模块到输出模块','', '技术架构')
        self.assertEqual(diagram['category'],'diagram')
