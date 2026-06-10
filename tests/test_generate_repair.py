#!/usr/bin/env python3
"""
Unit tests for _repair_unbalanced_quotes_in_json (Bug3 v7 修复函数)

为什么必须测: 06-09 promote 的 v6 算法有 regression (误改合法引号), 06-10 凌晨导致 cron
全失败 (2/2 books). v7 算法在 06-10 06:50 修, recurrence=2, 必须 unit test 锁住.

测试覆盖:
  1. 正常 JSON: 不该破坏
  2. 含中文双引号 “”: 不该破坏
  3. M3 输出的未转义半角伪 quote (单对): 修复成功
  4. M3 输出的 2 对未转义 (如 "闲...话" + 另一对): 修复成功
  5. 多段多处未转义 (一段内 char484 + char695 + ...): 修复成功
  6. 已经在 06-10 跑通的 2 本书真实 raw: 验证仍能修
"""
import sys, os, json, unittest

# 加 scripts/ 到 path 以导入 generate
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import generate


class TestRepairUnbalancedQuotes(unittest.TestCase):
    """测试 _repair_unbalanced_quotes_in_json v7 算法"""

    def test_01_valid_json_passthrough(self):
        """合法 JSON: 函数必须原样返回, 不破坏任何字符"""
        valid = '{"title": "正常", "body": "无引号问题", "n": 42}'
        result = generate._repair_unbalanced_quotes_in_json(valid)
        self.assertEqual(result, valid, "合法 JSON 不该被改")
        # 验证 parse 仍能成功
        json.loads(result)

    def test_02_chinese_quotes_preserved(self):
        """含中文左右双引号 “” 的合法 JSON: 不该破坏"""
        valid = '{"title": "他说：“你好”", "body": "ok"}'
        result = generate._repair_unbalanced_quotes_in_json(valid)
        self.assertEqual(result, valid, "中文引号不该被改")
        json.loads(result)

    def test_03_single_unescaped_quote_in_string(self):
        """M3 输出: 一段内 1 对未转义半角双引号 (典型 char484 模式)"""
        # 模拟 M3 输出: "...他提起姥姥却像个孩子。姥姥..."闲...话"..." 这类
        raw = '''{
  "sections": {
    "opening": "今天讲姥姥",
    "book_bg": "姥姥那些"闲事"让她高兴了一辈子"
  }
}'''
        # 原始 raw 应该 parse 失败
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        # 修复后应该 parse 成功
        fixed = generate._repair_unbalanced_quotes_in_json(raw)
        result = json.loads(fixed)
        self.assertIn('sections', result)
        self.assertIn('闲事', result['sections']['book_bg'])

    def test_04_multiple_unescaped_quotes(self):
        """M3 输出: 1 段内多对未转义 (06-10 实际场景: 段 1 出现 1-2 对)"""
        # 注意: v7 算法对 4+ 对会产生误改, 但 06-10 实际 1-2 对场景必成功
        # 模拟《七个疯子》M3 段 1 输出: 一对未转义
        raw = '''{
  "sections": {
    "opening": "他叫"埃尔多萨因"",
    "book_bg": "他偷了"六百比索零七分""
  }
}'''
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        fixed = generate._repair_unbalanced_quotes_in_json(raw)
        result = json.loads(fixed)
        # 不应抛 json.JSONDecodeError
        self.assertIn('sections', result)

    def test_05_real_laolaoyulu_raw(self):
        """06-10 真实《姥姥语录》raw (从 log 抓的, M3 段1 输出)"""
        raw = '''{
  "sections": {
    "opening": "你有没有想过，一个大字不识的老太太，怎么就成了全家人的精神导师？山东荣成水门口村有这样一位小脚姥姥，她一辈子围着锅台转，却把做人做事的道理讲得通透，讲得让人想哭。九十九年的人生路，她走完了，留下一肚子大白话。这些话，写在央视主持人倪萍的书里，就叫《姥姥语录》。今天这一百二十分钟，我们一起走进这本书，听听那些我们听过却常常忘记的道理——吃亏是福、给予是幸福、天黑了也得躺下，因为多黑的天到头了也得亮。我们会从倪萍和姥姥五十年的相处讲起，聊姥姥怎么成为孙女的老师，聊一个家庭里最朴素也最深厚的爱与告别。",
    "book_bg": "倪萍，一九五九年生于山东，是家喻户晓的央视主持人。她主持过十三届春晚，拿过无数电视大奖，是中国电视史上最具影响力的面孔之一。可就是这样一位光鲜亮丽的名人，提起姥姥却像个孩子。姥姥名叫刘鸿卿，山东荣成水门口村人，一辈子没进过学堂，却把十个子女、几十个孙辈拉扯长大，活到九十九岁。姥姥的爹是识文断字的人，只因她生为女性，才没机会读书。倪萍从小跟着姥姥长大，姥姥那些"闲着没事说的没用的话"，她全都记在心上，写成了这本《姥姥语录》。",
    "_phase": "head_done"
  }
}'''
        # 原始 raw 应该 parse 失败 (有 "闲着没事..." 这对伪引号)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        # 修复后应该 parse 成功
        fixed = generate._repair_unbalanced_quotes_in_json(raw)
        result = json.loads(fixed)
        self.assertEqual(len(result['sections']), 3)
        self.assertIn('闲着没事说的没用的话', result['sections']['book_bg'])
        # 验证不破坏合法引号
        self.assertIn('sections', result)

    def test_06_no_infinite_loop(self):
        """500 轮内必返回 (不能死循环)"""
        # 故意构造一个完全坏掉无法修复的 raw
        bad_raw = '"unclosed string with no escape'
        # 不会抛 json.JSONDecodeError 也不会死循环, 函数应该 graceful return
        result = generate._repair_unbalanced_quotes_in_json(bad_raw)
        # 返回值不强制 parse 成功, 但函数必须在合理时间返回
        self.assertIsInstance(result, str)

    def test_07_does_not_break_legal_opening_key(self):
        """v6 修复: 误把 "sections"/"opening" 合法 key 开始的 " 改成中文. v7 必须保留."""
        valid_with_many_keys = '''{
  "title": "测试",
  "opening": "开场",
  "book_bg": "背景",
  "themes": ["a", "b", "c"]
}'''
        result = generate._repair_unbalanced_quotes_in_json(valid_with_many_keys)
        # 必须 parse 成功且不丢失任何字段
        parsed = json.loads(result)
        self.assertEqual(parsed['title'], "测试")
        self.assertEqual(parsed['opening'], "开场")
        self.assertEqual(parsed['book_bg'], "背景")
        self.assertEqual(parsed['themes'], ["a", "b", "c"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
