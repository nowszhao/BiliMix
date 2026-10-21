> 人物与话语约束：读取源行 speaker/turn 注释。允许同人相邻源行合并为完整话语；禁止跨真实人物合成一句。不要为凑词数强切一句未结束的话。没有人物数据时先跑预分析，不凭文本猜声纹。保留全部源词，ASR 纠错仍留给翻译步骤。

# 字幕整理指令（Refine Prompt Template）

本文件是给**执行字幕整理（语义重断句）的 Agent（任何内置模型的 Agent）** 的完整指令。
这是翻译前的独立一步：把 `aggregate_srt.py` 产出的**超长行**（词数过多或时长过长）按语义断成
2-4 个短子句，让每一行既读得完、语义又完整，为后续逐行翻译打基础。

按块整理时，把本文件连同 `refine_parts/refine_part_NN.txt` 一起交给 Agent，
要求其把整理结果写回 `refine_parts/refined_NN.txt`。

---

## 你的任务

输入是 `refine_parts/refine_part_NN.txt`，结构如下：

```
# === GLOBAL CONTEXT (reference only — do NOT translate) ===
# Video: <视频标题>
# URL: <视频链接>
# Duration: ...
# Subtitle source: auto-generated ASR (en), aggregated sentence-level
# Task: re-split each over-long line below into semantically complete clauses.
#       Output ONE line per clause: <idx>\t<clause text>
#       Keep idx as-is; keep every word in order (only punctuation may be added);
#       2-4 clauses per line; 5-16 words per clause; NEVER output timestamps.
# === END GLOBAL CONTEXT ===
# === PREVIOUS CHUNK (reference only — do NOT translate) ===
# <前一批的末尾原文，用于衔接语境>
# === END PREVIOUS CHUNK ===
# === LINES TO RE-SPLIT (idx\tstart\tend\ttext) ===
<idx>	<start>	<end>	<超长行英文原文>
```

- 所有 `#` 开头的行是**上下文/说明，禁止改动、禁止输出**。
- `# === LINES TO RE-SPLIT ===` 之后才是要处理的内容，每行格式：
  `<idx>\t<start>\t<end>\t<英文原文>`。`start/end` 是该行的原始时间轴，**只用于
  感知时长，不允许输出到结果里**。

## 输出格式（硬规则，违反即失败）

对每一行要处理的内容，输出**若干行**，每行一个子句：

```
<idx>\t<子句1 英文>
<idx>\t<子句2 英文>
<idx>\t<子句3 英文>
```

规则：

1. **`idx` 原样保留**，同一行的多个子句共享同一个 idx（重复出现）。
2. **词序不变，实词一个都不能增、删、改**。这是硬约束——脚本依赖"子句词数之和 ≈
   原行词数"把每个子句映射回音频时间线。你只能**添加/调整标点**（原 ASR 无标点，
   给子句补上句号、逗号、问号让它读起来完整），不能改写、合并、删除任何单词。
3. 每个超长行断成 **2-4 个子句**，每子句以完整话语为先（5–16 词只是建议）。
4. 子句之间必须是**语义完整的断点**：子句 1 结尾和子句 2 开头各自能成句，或至少是
   自然的短语边界。不要机械地"每 N 词切一刀"——要用语义判断：
   - 优先在**从句边界、列举项边界、同位语前后、插入语前后**断开；
   - 句尾有悬空引导词（"and then"、"because"、"which"）时，把它挪到下一个子句开头，
     不要让子句以孤零零的连接词收尾；
   - 不要拆散完整的短语/专名（"OpenAI"、"Greg Brockman" 保持一体）。
5. **只处理 `# === LINES TO RE-SPLIT ===` 下列出的行**。不要对没列出的行做任何事。
6. **不输出时间戳**，不输出任何解释文字，不输出 `#` 上下文。
7. 如果你判断某行其实已经语义完整、无需断开，可以输出单行（`<idx>\t<原文>`），
   但这种情况应极少——被选中的行都是词数/时长超标的。

## 工作方式

1. 先读 GLOBAL CONTEXT，把握视频主题、说话人、语气。
2. 读 PREVIOUS CHUNK，知道当前内容承接什么语境。
3. 逐行通读要整理的内容，在心里划出语义边界，再落笔输出。
4. 输出前自查：每个子句 5-16 词？实词有没有被改/删？标点是否让子句读起来完整？
   断点是否在语义自然处（而不是"第 8 个词处"）？

## 为什么不能改词（对你很重要）

下游 `refine_srt.py apply` 用**词数比例**把每个子句映射回 YouTube ASR 的碎片时间线，
继承真实语音时间戳。你一旦增删实词，子句词数就和源对不上，时间戳会错位。
真正需要"改词"的事（ASR 纠错、TTS 净化）全部留给翻译步骤。

## 示例

输入行：
```
27	0:03:10.00	0:03:22.40	and so the way I think about this is you need to build the product first and then you can worry about scaling it out to millions of users that was my whole point in the talk
```

可接受的输出：
```
27	and so the way I think about this is you need to build the product first.
27	and then you can worry about scaling it out to millions of users.
27	that was my whole point in the talk.
```

注意：三个子句的词序与原文完全一致，只补了句号；断点都在语义自然处。

## 结果交付

把完整结果写入指定的 `refined_NN.txt` 文件（UTF-8，与输入同目录）。
只写这一个结果文件，不输出任何额外说明。
