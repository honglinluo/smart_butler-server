import re
from typing import Any
from urllib.request import urlopen
from bs4 import BeautifulSoup
from collections import defaultdict
import math

def tool_web_content_summary(url: str) -> dict:
    """
    从给定的网页抓取内容并生成摘要
    """
    try:
        response = urlopen(url)
        if response.getcode() != 200:
            return {"success": False, "result": None, "error": f"Failed to open URL: {response.status} - {response.reason}"}
        
        html = response.read().decode('utf-8')
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()
        text = re.sub(r'\s+', ' ', text).strip()

        sentences = [sentence for sentence in re.split('[.!?]', text) if len(sentence) > 5]
        
        def tf(word, sentence):
            return sentence.count(word) / len(sentence)
        
        def n_containing(word, sentences):
            return sum(1 for s in sentences if word in s)
        
        def idf(word, sentences):
            return math.log(len(sentences) / (1 + n_containing(word, sentences)))
        
        scores = defaultdict(int)
        for sentence in sentences:
            for word in set(sentence.split()):
                scores[sentence] += tf(word, sentence) * idf(word, sentences)
        
        summary_sentences = sorted(scores.items(), key=lambda x: -scores[x[0]])[:3]
        summary_text = ' '.join([s[0] for s in summary_sentences])
        
        return {"success": True, "result": {"summary": summary_text, "code": 200}, "error": ""}
    except Exception as e:
        return {"success": False, "result": None, "error": str(e)}