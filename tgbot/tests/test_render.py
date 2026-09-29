import re
import unittest

from render import LIMIT, plain, render

ALLOWED = {"b", "i", "s", "code", "pre", "a", "blockquote"}


def assert_valid(case, message):
    """Tags are Telegram-supported and balanced, so no part is rejected as unparsable."""
    stack = []
    for closing, name in re.findall(r"<(/?)([a-z]+)[^>]*>", message):
        case.assertIn(name, ALLOWED, message)
        if closing:
            case.assertEqual(stack.pop(), name, message)
        else:
            stack.append(name)
    case.assertEqual(stack, [], message)
    case.assertLessEqual(len(plain(message)), LIMIT)


class RenderTests(unittest.TestCase):
    def one(self, markdown):
        messages = render(markdown)
        self.assertEqual(len(messages), 1)
        assert_valid(self, messages[0])
        return messages[0]

    def test_typical_llm_answer(self):
        answer = """## Итог

Это **важно**, а это *курсив* и ~~старое~~. Вызов `f(x)`.

1. Первый пункт
2. Второй с [ссылкой](https://example.com/?a=1&b=2)
   - вложенный
   - еще

> Цитата

---

```python
if a < b and c > d:
    print("&")
```"""
        self.assertEqual(self.one(answer), """<b>Итог</b>

Это <b>важно</b>, а это <i>курсив</i> и <s>старое</s>. Вызов <code>f(x)</code>.

1. Первый пункт
2. Второй с <a href="https://example.com/?a=1&amp;b=2">ссылкой</a>
   • вложенный
   • еще

<blockquote>Цитата</blockquote>

——————

<pre><code class="language-python">if a &lt; b and c &gt; d:
    print("&amp;")</code></pre>""")

    def test_special_characters_are_escaped(self):
        self.assertEqual(self.one("a < b > c & d <b>raw</b> 2*3*4 snake_case_name."),
                         "a &lt; b &gt; c &amp; d &lt;b&gt;raw&lt;/b&gt; 2<i>3</i>4 snake_case_name.")
        self.assertEqual(self.one("<div>block html</div>"), "&lt;div&gt;block html&lt;/div&gt;")

    def test_unsafe_links_and_images_become_text(self):
        self.assertEqual(self.one("[x](ftp://host/f) ![alt](http://i.png)"), "x alt")
        self.assertNotIn("<a", self.one("[x](javascript:alert(1))"))

    def test_table_is_monospace(self):
        self.assertEqual(self.one("| Модель | VRAM |\n|---|---:|\n| qwen | 16 |\n| gemma<b> | 12 |"),
                         "<pre><code>Модель   | VRAM\n---------+-----\nqwen     | 16\ngemma&lt;b&gt; | 12</code></pre>")

    def test_nested_quotes_and_ordered_start(self):
        self.assertEqual(self.one("> a\n>> b"), "<blockquote>a\n\nb</blockquote>")
        self.assertEqual(self.one("3. c\n4. d"), "3. c\n4. d")

    def test_unclosed_markup_is_literal(self):
        self.assertEqual(self.one("**not closed and `tick"), "**not closed and `tick")
        self.assertEqual(self.one("```\nopen fence"), "<pre><code>open fence</code></pre>")

    def test_long_answer_splits_between_blocks(self):
        paragraphs = [f"**{i}** " + "слово " * 150 for i in range(12)]
        messages = render("\n\n".join(paragraphs))
        self.assertGreater(len(messages), 1)
        for message in messages:
            assert_valid(self, message)
        self.assertEqual(sum(plain(m).count("слово") for m in messages), 12 * 150)

    def test_long_code_block_splits_into_valid_pre(self):
        code = "\n".join(f"line {i} <tag> & more" for i in range(600))
        messages = render(f"Код:\n\n```js\n{code}\n```")
        self.assertGreater(len(messages), 2)
        for message in messages:
            assert_valid(self, message)
        joined = "\n".join(plain(m) for m in messages)
        self.assertEqual(joined.count("<tag>"), 600)
        self.assertTrue(all("language-js" in m for m in messages[1:]))

    def test_oversized_paragraph_and_line(self):
        for markdown in ("**" + "x" * 9000 + "**", "```\n" + "y" * 9000 + "\n```"):
            messages = render(markdown)
            for message in messages:
                assert_valid(self, message)
            self.assertGreater(len(messages), 2)
            self.assertEqual(sum(len(plain(m)) for m in messages), 9000)

    def test_empty(self):
        self.assertEqual(render(""), ["(пустой ответ)"])
        self.assertEqual(render("   \n"), ["(пустой ответ)"])


if __name__ == "__main__":
    unittest.main()
