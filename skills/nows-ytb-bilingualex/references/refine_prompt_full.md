> 音频优先协议 v1：读取源行 speaker/turn 注释。允许同人相邻源行合并为完整话语；禁止跨真实人物合成一句。不要为凑词数强切一句未结束的话。没有人物数据时先跑预分析，不凭文本猜声纹。保留全部源词的规则不变，ASR 纠错仍留给翻译步骤。

# 全量字幕整理指令（Refine Prompt Template — FULL MODE）

本文件是给**执行全量语义重断句的 Agent（任何内置模型的 Agent）** 的完整指令。
这是翻译前的独立一步：把 `aggregate_srt.py` 机械聚合出的**全部字幕行**按语义重写成
语义完整、长度适中的句子——可以**保持**、**拆分**或**跨行合并**，让断句完全由语义
驱动（机械断句把一句话切两半的情况在这里被修复）。

按块处理时，把本文件连同 `refine_parts/refine_part_NN.txt` 一起交给 Agent，
把结果写回 `refine_parts/refined_NN.txt`。

---

## 你的任务

输入是 `refine_parts/refine_part_NN.txt`，结构如下：

```
# === GLOBAL CONTEXT (reference only — do NOT translate) ===
# Video: <视频标题>
# URL: <视频链接>
# Duration: ...
# Task: rewrite the WHOLE block below into semantically complete sentences.
#       For each line you may KEEP it, SPLIT it into several clauses, or MERGE
#       it with the NEXT line.
# Output ONE line per final sentence: S<seq>\t<sentence text>
#       (seq = 1,2,3... in video order, one output line per sentence)
# Keep every word in order across the block (only punctuation may be added);
#       Prefer complete utterances; 5-16 words is a soft guide; NEVER output timestamps.
# === END GLOBAL CONTEXT ===
# === PREVIOUS CHUNK (reference only — do NOT translate) ===
# <前一批的末尾原文，用于衔接语境>
# === END PREVIOUS CHUNK ===
# === LINES TO REWRITE (idx\tstart\tend\ttext) ===
<idx>	<start>	<end>	<英文原文>
...
```

- 所有 `#` 开头的行是**上下文/说明，禁止改动、禁止输出**。
- `# === LINES TO REWRITE ===` 之后是**本块要重写的全部行**，每行格式：
  `<idx>\t<start>\t<end>\t<英文原文>`。`start/end` 只用于感知时长，禁止输出。
- **块内行号是连续的**（如 41-80）。你可以跨行合并，即一个句子可以包含第 41 行
  和第 42 行的词。

## 输出格式（硬规则，违反即失败）

对整块输出**重写后的完整句子列表**，每行一个句子：

```
S1	<句子1 英文>
S2	<句子2 英文>
S3	<句子3 英文>
...
```

`S<seq>` 从 1 开始连续编号，**按视频顺序**排列（seq 越大越靠后）。

规则：

0. **一个词都不能删（最高优先，先于一切其他规则）**。这包括：语气词（`uh`、`um`、
   `like`）、重复词（`the the`、`to to`、`and and`）、连接词（`and`、`to`、`so`）——
   **全部原样保留在输出句子里**。重写句子时你会本能地想"清理"这些词，但脚本依赖
   "句子词流 = 源词流"做时间戳锚定，删掉任何一词都会让下游时间轴塌缩（实测
   FULL 模式约 20% 的分块因此返工）。判断标准：输出句子的词集与源行词集必须
   **100% 相等**，一个不多一个不少。
1. **词序不变，实词一个都不能增、删、改**。这是硬约束——脚本依赖"句子词数与源词
   流对齐"把每个句子映射回音频时间线。你只能**添加/调整标点**（原 ASR 无标点，
   给句子补上句号、逗号、问号），不能改写、合并、删除任何单词。真正的改词
   （ASR 纠错）留给翻译步骤。
2. 每个句子优先语义完整；5–16 词只是建议。短答可更短，完整长句可更长。
3. **断句/合并决策由语义驱动**：
   - 一个完整句子被机械断成两行 → **合并**成一句；
   - 一行里包含多个独立子句 → **拆分**成多句；
   - 一行本来就完整且长度合适 → **保持**原样（输出一句，词不变）；
   - 断点优先在**从句边界、列举项边界、同位语前后、插入语前后**；
   - 句尾有悬空引导词（"and then"、"because"、"which"）时，把它挪到下一个句子
     开头，不要让句子以孤零零的连接词收尾；
   - 不要拆散完整的短语/专名（"OpenAI"、"Greg Brockman" 保持一体）。
4. **只能合并相邻行**。不得跨块合并（PREVIOUS CHUNK 只是参考）。
5. **不输出时间戳**，不输出任何解释文字，不输出 `#` 上下文。
6. 输出句子必须覆盖块内**全部**单词——不能漏词（漏词 = 内容缺失）。

## 工作方式

1. 先读 GLOBAL CONTEXT，把握视频主题、说话人、语气。
2. 读 PREVIOUS CHUNK，知道当前内容承接什么语境。
3. 通读整块，在脑子里把每一句完整的话划出来（可能跨源行），再落笔输出。
4. 输出前自查：每句话语义是否完整？实词有没有被改/删？**语气词/重复词（uh/um/the the）
   有没有被"顺手清理"掉（这是最常见错误，必须 100% 保留）**？有没有漏词？断点/合并
   是否在语义自然处？句子顺序是否与源一致？

## 为什么不能改词（对你很重要）

下游严格校验源词顺序、人物归属和完整覆盖，再继承词级时间戳。增删改词会使校验失败，必须修复原模型输出后才能继续。ASR 纠错和 TTS 文本净化留给精翻与中文话语组装阶段。

## 示例

块内输入（节选）：
```
13	0:00:57,600	0:01:04,600	Let me show you how. By the end of the session, I want you to leave with a clear understanding of what
14	0:01:04,600	0:01:10,000	ontology means and how we make it work in practice. So let's dive in.
```

这两个源行其实包含**三句完整的话**，机械断句恰好把它们切开了，可接受的输出：

```
S7	Let me show you how.
S8	By the end of the session, I want you to leave with a clear understanding of what ontology means and how we make it work in practice.
S9	So let's dive in.
```

注意：词序与原文完全一致，只补了标点；完整长句跨源行合并，全部词保持原顺序。

## 结果交付

把完整结果写入指定的 `refined_NN.txt` 文件（UTF-8，与输入同目录）。
只写这一个结果文件，不输出任何额外说明。
