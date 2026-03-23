#!/usr/bin/env python3
"""
ArXiv 论文搜索脚本 - 简化版
根据日期范围搜索论文，并根据研究兴趣进行筛选和评分
"""

import xml.etree.ElementTree as ET
import json
import re
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional, Tuple
from pathlib import Path
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    logger.warning("requests library not found, using urllib")

# ---------------------------------------------------------------------------
# 配置路径
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 配置文件在 scripts 目录的上一级
CONFIG_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "research_config.md")
OUTPUT_DIR = "/Users/zqy/Desktop/zqy/Github/claude/arxiv_paper"

# arXiv API 配置
ARXIV_NS = {
    'atom': 'http://www.w3.org/2005/Atom',
    'arxiv': 'http://arxiv.org/schemas/atom'
}

# 评分常量
SCORE_MAX = 3.0
RELEVANCE_TITLE_KEYWORD_BOOST = 0.5
RELEVANCE_SUMMARY_KEYWORD_BOOST = 0.3
RECENCY_THRESHOLDS = [
    (30, 3.0),
    (90, 2.0),
    (180, 1.0),
]
RECENCY_DEFAULT = 0.0

# 权重
WEIGHTS = {
    'relevance': 0.40,
    'recency': 0.20,
    'popularity': 0.30,
    'quality': 0.10,
}


def parse_config(config_path: str) -> Dict:
    """解析配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        content = f.read()

    config = {
        'keywords': [],
        'arxiv_categories': ['cs.CV', 'cs.AI', 'cs.LG', 'cs.CL'],
        'output_dir': OUTPUT_DIR,
        'top_n': 10,
        'excluded_keywords': ['workshop', 'survey', 'review', 'position paper'],
    }

    # 解析研究方向和关键词 - 提取 ### 标题下的列表项
    lines = content.split('\n')
    in_research_section = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # 检查是否在研究方向部分
        if stripped.startswith('### ') and '(' in stripped:
            in_research_section = True
            continue

        # 如果遇到 ## 搜索配置，停止收集关键词
        if stripped.startswith('## '):
            in_research_section = False
            continue

        # 收集关键词（以 - 开头）
        if in_research_section and stripped.startswith('- '):
            keyword = stripped[2:].strip()
            if keyword and not keyword.startswith('['):
                config['keywords'].append(keyword)

        # 解析配置项
        if stripped.startswith('### '):
            if 'arXiv 分类' in stripped:
                # 接下来几行是分类
                for j in range(i+1, min(i+10, len(lines))):
                    cat_line = lines[j].strip()
                    if cat_line.startswith('cs.'):
                        if cat_line not in config['arxiv_categories']:
                            config['arxiv_categories'].append(cat_line)
                    elif cat_line.startswith('##') or cat_line.startswith('###'):
                        break

    # 去重关键词
    config['keywords'] = list(set(config['keywords']))

    return config


def search_arxiv_by_date_range(
    categories: List[str],
    start_date: datetime,
    end_date: datetime,
    max_results: int = 200,
    max_retries: int = 3
) -> List[Dict]:
    """搜索指定日期范围的论文"""
    category_query = "+OR+".join([f"cat:{cat}" for cat in categories])
    date_query = f"submittedDate:[{start_date.strftime('%Y%m%d')}0000+TO+{end_date.strftime('%Y%m%d')}2359]"
    full_query = f"({category_query})+AND+{date_query}"

    url = (
        f"http://export.arxiv.org/api/query?"
        f"search_query={full_query}&"
        f"max_results={max_results}&"
        f"sortBy=submittedDate&"
        f"sortOrder=descending"
    )

    logger.info("[arXiv] Searching papers from %s to %s", start_date.date(), end_date.date())

    for attempt in range(max_retries):
        try:
            if HAS_REQUESTS:
                response = requests.get(url, timeout=60)
                response.raise_for_status()
                xml_content = response.text
            else:
                with urllib.request.urlopen(url, timeout=60) as response:
                    xml_content = response.read().decode('utf-8')

            papers = parse_arxiv_xml(xml_content)
            logger.info("[arXiv] Found %d papers", len(papers))
            return papers
        except Exception as e:
            logger.warning("[arXiv] Error (attempt %d/%d): %s", attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 5
                logger.info("[arXiv] Retrying in %d seconds...", wait_time)
                time.sleep(wait_time)
            else:
                logger.error("[arXiv] Failed after %d attempts", max_retries)
                return []

    return []


def parse_arxiv_xml(xml_content: str) -> List[Dict]:
    """解析 arXiv XML 结果"""
    papers = []

    try:
        root = ET.fromstring(xml_content)

        for entry in root.findall('atom:entry', ARXIV_NS):
            paper = {}

            # ID
            id_elem = entry.find('atom:id', ARXIV_NS)
            if id_elem is not None:
                paper['id'] = id_elem.text
                # 支持两种格式: arXiv:xxxx.xxxxx 或 http://arxiv.org/abs/xxxx.xxxxxv1
                match = re.search(r'(?:arXiv:)?(\d+\.\d+)v?\d*', paper['id'])
                if match:
                    paper['arxiv_id'] = match.group(1)

            # 标题
            title_elem = entry.find('atom:title', ARXIV_NS)
            if title_elem is not None:
                paper['title'] = title_elem.text.strip()

            # 摘要
            summary_elem = entry.find('atom:summary', ARXIV_NS)
            if summary_elem is not None:
                paper['summary'] = summary_elem.text.strip()

            # 作者
            authors = []
            for author in entry.findall('atom:author', ARXIV_NS):
                name_elem = author.find('atom:name', ARXIV_NS)
                if name_elem is not None:
                    authors.append(name_elem.text)
            paper['authors'] = authors

            # 发布日期
            published_elem = entry.find('atom:published', ARXIV_NS)
            if published_elem is not None:
                paper['published'] = published_elem.text
                try:
                    paper['published_date'] = datetime.fromisoformat(
                        paper['published'].replace('Z', '+00:00')
                    )
                except:
                    paper['published_date'] = None

            # 分类
            categories = []
            for category in entry.findall('atom:category', ARXIV_NS):
                term = category.get('term')
                if term:
                    categories.append(term)
            paper['categories'] = categories

            # PDF 链接
            for link in entry.findall('atom:link', ARXIV_NS):
                if link.get('title') == 'pdf':
                    paper['pdf_url'] = link.get('href')
                    break

            if 'id' in paper:
                paper['url'] = paper['id']

            paper['source'] = 'arxiv'

            papers.append(paper)

    except ET.ParseError as e:
        logger.error("Error parsing XML: %s", e)

    return papers


def calculate_relevance_score(paper: Dict, keywords: List[str], excluded: List[str]) -> Tuple[float, List[str]]:
    """计算相关性评分"""
    title = paper.get('title', '').lower()
    summary = paper.get('summary', '').lower()

    # 检查排除关键词
    for keyword in excluded:
        if keyword.lower() in title or keyword.lower() in summary:
            return 0, []

    score = 0
    matched_keywords = []

    for keyword in keywords:
        keyword_lower = keyword.lower()
        if keyword_lower in title:
            score += RELEVANCE_TITLE_KEYWORD_BOOST
            matched_keywords.append(keyword)
        elif keyword_lower in summary:
            score += RELEVANCE_SUMMARY_KEYWORD_BOOST
            matched_keywords.append(keyword)

    return score, matched_keywords


def calculate_recency_score(published_date: Optional[datetime]) -> float:
    """计算新近性评分"""
    if published_date is None:
        return 0

    now = datetime.now(published_date.tzinfo) if published_date.tzinfo else datetime.now()
    days_diff = (now - published_date).days

    for max_days, score in RECENCY_THRESHOLDS:
        if days_diff <= max_days:
            return score
    return RECENCY_DEFAULT


def calculate_quality_score(summary: str) -> float:
    """计算质量评分"""
    score = 0.0
    summary_lower = summary.lower()

    strong_innovation = ['state-of-the-art', 'sota', 'breakthrough', 'first', 'surpass', 'outperform', 'pioneering']
    weak_innovation = ['novel', 'propose', 'introduce', 'new approach', 'new method', 'innovative']
    method_indicators = ['framework', 'architecture', 'algorithm', 'mechanism', 'pipeline', 'end-to-end']
    quantitative_indicators = ['outperforms', 'improves by', 'achieves', 'accuracy', 'f1', 'bleu', 'rouge', 'beats', 'surpasses']

    strong_count = sum(1 for ind in strong_innovation if ind in summary_lower)
    if strong_count >= 2:
        score += 1.0
    elif strong_count == 1:
        score += 0.7
    else:
        weak_count = sum(1 for ind in weak_innovation if ind in summary_lower)
        if weak_count > 0:
            score += 0.3

    if any(ind in summary_lower for ind in method_indicators):
        score += 0.5

    if any(ind in summary_lower for ind in quantitative_indicators):
        score += 0.8

    return min(score, SCORE_MAX)


def calculate_recommendation_score(relevance: float, recency: float, popularity: float, quality: float) -> float:
    """计算综合推荐评分"""
    normalized = {k: (v / SCORE_MAX) * 10 for k, v in {
        'relevance': relevance,
        'recency': recency,
        'popularity': popularity,
        'quality': quality
    }.items()}

    final_score = sum(normalized[k] * WEIGHTS[k] for k in WEIGHTS)
    return round(final_score, 2)


def filter_and_score_papers(papers: List[Dict], config: Dict, target_date: datetime) -> List[Dict]:
    """筛选和评分论文"""
    keywords = config.get('keywords', [])
    excluded = config.get('excluded_keywords', [])

    scored_papers = []

    for paper in papers:
        relevance, matched_keywords = calculate_relevance_score(paper, keywords, excluded)
        if relevance == 0:
            continue

        recency = calculate_recency_score(paper.get('published_date'))

        # 基于新近性的热门度
        if paper.get('published_date'):
            pub = paper['published_date']
            now = datetime.now(pub.tzinfo) if pub.tzinfo else datetime.now()
            days_old = (now - pub).days
            if days_old <= 7:
                popularity = 2.0
            elif days_old <= 14:
                popularity = 1.5
            elif days_old <= 30:
                popularity = 1.0
            else:
                popularity = 0.5
        else:
            popularity = 0.5

        quality = calculate_quality_score(paper.get('summary', ''))

        recommendation_score = calculate_recommendation_score(relevance, recency, popularity, quality)

        paper['scores'] = {
            'relevance': round(relevance, 2),
            'recency': round(recency, 2),
            'popularity': round(popularity, 2),
            'quality': round(quality, 2),
            'recommendation': recommendation_score
        }
        paper['matched_keywords'] = matched_keywords

        scored_papers.append(paper)

    scored_papers.sort(key=lambda x: x['scores']['recommendation'], reverse=True)
    return scored_papers


def generate_markdown(papers: List[Dict], start_date: str, end_date: str, config: Dict) -> str:
    """生成 Markdown 格式的输出"""
    md_lines = []

    # 标题
    md_lines.append(f"# ArXiv 论文推荐: {start_date} - {end_date}")
    md_lines.append("")
    md_lines.append(f"**搜索日期**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md_lines.append(f"**时间范围**: {start_date} 至 {end_date}")
    md_lines.append(f"**论文数量**: {len(papers)} 篇")
    md_lines.append("")

    for i, paper in enumerate(papers, 1):
        md_lines.append(f"## {i}. {paper.get('title', 'N/A')}")
        md_lines.append("")

        # 基本信息
        arxiv_id = paper.get('arxiv_id', 'N/A')
        authors = ', '.join(paper.get('authors', [])[:5])
        if len(paper.get('authors', [])) > 5:
            authors += ' et al.'

        md_lines.append(f"**arXiv ID**: {arxiv_id}")
        md_lines.append(f"**作者**: {authors}")
        md_lines.append(f"**发布时间**: {paper.get('published', 'N/A')[:10]}")
        md_lines.append(f"**链接**: [arXiv]({paper.get('url', '#')}) | [PDF]({paper.get('pdf_url', '#')})")
        md_lines.append("")

        # 评分
        scores = paper.get('scores', {})
        md_lines.append(f"**相关性评分**: {scores.get('relevance', 0):.2f}/3.0 ({scores.get('recommendation', 0):.1f}/10)")
        md_lines.append(f"- 相关性: {scores.get('relevance', 0):.2f}")
        md_lines.append(f"- 新近性: {scores.get('recency', 0):.2f}")
        md_lines.append(f"- 热门度: {scores.get('popularity', 0):.2f}")
        md_lines.append(f"- 质量: {scores.get('quality', 0):.2f}")
        md_lines.append("")

        # 匹配的关键词
        if paper.get('matched_keywords'):
            md_lines.append(f"**匹配关键词**: {', '.join(paper.get('matched_keywords', [])[:5])}")
            md_lines.append("")

        # 摘要 - 只保存英文原文
        summary = paper.get('summary', '')
        md_lines.append("### 摘要")
        md_lines.append(summary)
        md_lines.append("")

        # 简介 - 先留空，等待后续填充
        md_lines.append("### 简介")
        md_lines.append("*（待补充）*")
        md_lines.append("")

        md_lines.append("---")
        md_lines.append("")

    return '\n'.join(md_lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description='ArXiv论文搜索')
    parser.add_argument('--start-date', type=str, required=True, help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end-date', type=str, required=True, help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--top-n', type=int, default=10, help='返回论文数量')
    parser.add_argument('--config', type=str, default=CONFIG_PATH, help='配置文件路径')
    parser.add_argument('--output-dir', type=str, default=OUTPUT_DIR, help='输出目录')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    # 解析日期
    try:
        start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
        end_date = datetime.strptime(args.end_date, '%Y-%m-%d')
    except ValueError as e:
        logger.error("日期格式错误: %s", e)
        return 1

    # 加载配置
    if not os.path.exists(args.config):
        logger.error("配置文件不存在: %s", args.config)
        return 1

    config = parse_config(args.config)
    config['top_n'] = args.top_n

    logger.info("Loading config from: %s", args.config)
    logger.info("Keywords: %s", config['keywords'][:5])

    # 搜索论文
    papers = search_arxiv_by_date_range(
        categories=config['arxiv_categories'],
        start_date=start_date,
        end_date=end_date,
        max_results=200
    )

    if not papers:
        logger.warning("未找到论文")
        return 1

    # 筛选和评分
    scored_papers = filter_and_score_papers(papers, config, end_date)

    if not scored_papers:
        logger.warning("没有符合条件的论文")
        return 1

    # 取前 N 篇
    top_papers = scored_papers[:config['top_n']]

    # 生成输出目录
    os.makedirs(args.output_dir, exist_ok=True)

    # 生成文件名
    output_filename = f"papers_{args.start_date}_{args.end_date}.md"
    output_path = os.path.join(args.output_dir, output_filename)

    # 生成 Markdown
    md_content = generate_markdown(top_papers, args.start_date, args.end_date, config)

    # 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(md_content)

    logger.info("Results saved to: %s", output_path)

    # 同时输出 JSON 结果（供后续处理使用）
    result = {
        'start_date': args.start_date,
        'end_date': args.end_date,
        'total_found': len(papers),
        'total_filtered': len(scored_papers),
        'output_path': output_path,
        'papers': top_papers
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    return 0


if __name__ == '__main__':
    sys.exit(main())
