"""番号识别引擎 v3 —— 移植自 X:\\hermes\\library\\code_extract3.py
（源实现在 26,230 个真实文件名上做过全量实测迭代）。

框架契约（C1/C4 依赖，不得改）::

    NumberMatcher(rules).match(text) -> [
        {"number": str, "confidence": int, "source": str}, ...
    ]

v2 -> v3 修掉的三类缺陷
------------------------------------------------------------
1. 捕获组陷阱（P1-2 根因，是**类**缺陷不是 FC2 特有）
   v2 用 ``re.findall(pattern, text)``；Python 在 pattern 含捕获组时只返回
   **组的内容**，不返回整段匹配。FC2 规则 ``FC2[-_ ]?(PPV)?[-_ ]?\\d{3,8}``
   因此把番号变成 ``'PPV'``（FC2-PPV-1234567）或 ``''``（FC2-1234567）。
   v3 一律 ``re.finditer`` + ``m.group(0)`` 取整段匹配。

2. 覆盖率
   字典 13 条规则 -> 通用厂牌兜底 + 无厂牌数字番号 + tnum / numpfx / 118 系结构。

3. 噪音
   字幕组 / 站名 / 技术标记 / 欧美点分名 会污染匹配，先剥后匹配
   （``strip_noise`` 顺序敏感：TECH 必须先于 SITE_DOT）。

置信度刻度（int，越大越可信）
------------------------------------------------------------
D2「一个文件解析出多个候选 -> 入库 confidence 最高的那条」依赖这个全序：

====  ==============================================================
 100  FC2 双形态（FC2-PPV-{n} / FC2-{n}）；字典中 priority >= 100 的规则
  90  high    通用厂牌规则且厂牌在 KNOWN_STUDIO；字典 priority 90-99
  80  字典 priority 80-89
  70  medium  通用厂牌规则（厂牌不在 KNOWN_STUDIO）/ tnum 结构
  60  numpfx  数字前缀结构（300MAAN-403 形态）
  50  low     无分隔 nosep 形态
  40  bare    纯数字无厂牌番号（7 位）
  ====  ==============================================================

与源实现的两处**有意偏差**（已在交付说明中记录）
------------------------------------------------------------
a. 源 ``_norm()`` 会 ``lstrip('0')`` 去前导零（``SSIS-001`` -> ``SSIS-1``）。
   本移植**保留前导零**：框架既有行为如此，C1/C4 下游按 ``SSIS-001`` 形态
   查库；JavDB 等外部源也用零填充形态。
b. 置信度用 int 而非源实现的 ``'high'/'medium'/'low'`` 字符串 —— 保持框架
   既有类型，且字符串序（medium > low > high）与「越大越好」相反，会坑 D2。
"""

import os
import re

# ── 一、非 JAV 判定（这些省不出空间，直接忽略）──────────────────

NON_JAV = re.compile(
    r'(?i)('
    # 大平台
    r'pornhub|xvideos|xhamster|redtube|youporn|spankbang|'
    # 欧美写真/艺术站（无番号体系）
    r'watch4beauty|wowgirls|xart|metart|femjoy|hegre|eroticbeauty|'
    r'mplstudios|stunning18|beautyisdivine|stasyq|rylsky|eternaldesire|'
    r'ultra-?film|w4b|nubiles|teendreams|'
    # 直播/付费订阅
    r'onlyfans|fansly|manyvids|chaturbate|stripchat|camsoda|myfreecams|'
    r'livejasmin|bongacams|cam4|of-?hell|'
    # 流媒体镜像站
    r'jable|avgle|missav|sextb|supjav|123av|'
    # 素人创作者
    r'june\s*liu|刘玥|spicygum|'
    r'\bclip\d*\b'
    r')'
)

# 欧美命名：点分小写词组（maria.pie.and.mia.sports.star）
WESTERN_NAME = re.compile(r'(?i)\b[a-z]{2,}\.[a-z]{2,}\.[a-z]{2,}\b')

# 字幕组 / 动画圈
FANSUB = re.compile(
    r'(字幕组|字幕組|桜都|夜桜|ばにぃ|魔人|Okazu|Taka\.?Sub|'
    r'[\u3040-\u30ff]{4,})'
)

CAMERA = re.compile(r'(?i)^(IMG|VID|DSC|MVI|MOV|PXL|GOPR|screen)[-_ ]?\d')
PURE_NUM = re.compile(r'^\d{1,4}([-_.]\d{1,4})*$')
CJK = re.compile(r'[\u4e00-\u9fff]')

# 罗马数字重命名（无厂牌可抓）
ROMAN = re.compile(r'^[\s\-_]*[IVXLCDM]{3,}[\s\-_\d]*$')

# 3D 动画/游戏素材特征词
ANIM_WORD = re.compile(
    r'(?i)\b(breeding\s+session|mainmovie|elf|overwatch|dva|widow|pharah|mercy|'
    r'christmas|animation|blender|sfm|mmd)\b'
)

# 「像番号」豁免：字幕组/动画词命中，但串里带番号形态时不算非 JAV
LIKE_NUMBER = re.compile(r'(?i)[A-Za-z]{2,6}[-_ ]?\d{2,5}')
LIKE_NUMBER_STRICT = re.compile(r'(?i)[A-Za-z]{2,6}[-_]\d{2,5}')


def is_non_jav(name):
    """八类非 JAV 排除。返回 ``(是否非 JAV, 命中类别)``。"""

    stem = os.path.splitext(name)[0]

    # 「像番号」豁免：西文点分名 / 字幕组 / 动画素材词偶尔会和真番号同现，
    # 例如 `IPX-123-C.c-CDI.uncensored.leak` —— 剥掉噪音后番号很明确，
    # 不该因为 `CDI.UNCENSORED.LEAK` 长得像三段点分名就把整条丢掉。
    # 判据用**严格**形态 `[A-Za-z]{2,6}[-_]\d{2,5}`（带分隔符），
    # 避免把 `maria.pie.and.mia.sports.star` 这类真西文名误救回来。
    numbered = bool(LIKE_NUMBER_STRICT.search(stem))

    if NON_JAV.search(stem):
        return True, 'platform'
    if WESTERN_NAME.search(stem) and not numbered:
        return True, 'western'
    if FANSUB.search(stem) and not LIKE_NUMBER.search(stem):
        return True, 'anime'
    if ANIM_WORD.search(stem) and not numbered:
        return True, 'anime'
    if CAMERA.match(stem):
        return True, 'camera'
    if PURE_NUM.match(stem.strip()):
        return True, 'number'
    # 罗马数字判定的豁免：真罗马数字重命名（MDLXII / XLVII / 雨CCLIII）不含番号形态；
    # 而 `MIDV-123` 的 M/I/D/V 全落在 [IVXLCDM] 里，会被 ROMAN 误吞，
    # 连带 MIDV 这类厂牌的**所有**番号被静默丢弃（全量语料实测）。
    if ROMAN.match(stem) and not LIKE_NUMBER.search(stem):
        return True, 'roman'
    if len(CJK.findall(stem)) >= 10 and not LIKE_NUMBER_STRICT.search(stem):
        return True, 'amateur'

    return False, ''


# ── 二、噪音剥除（顺序敏感：TECH 必须先于 SITE_DOT）─────────────

BRACKET = re.compile(r'[\[【（\(]([^\]】）\)]{0,60})[\]】）\)]')

# 站名：必须以【字母】开头，长度受限 —— 防止从数字开始吞掉番号数字
SITE_DOT = re.compile(
    r'(?i)\b[a-z][a-z0-9\-]{0,20}\.(com|net|cc|tv|vip|org|me|xyz|club|top|info)\b'
)
# 数字域名单独处理（2048.cc / 1024dz.com）
SITE_NUMDOM = re.compile(r'(?i)\b(2048|1024|118|98t|91)\w{0,4}\.(com|cc|net|tv|vip)\b')
SITE_TILDE = re.compile(r'(?i)~[a-z0-9\-]{2,20}(\.(com|net|cc|vip))?')
BARE_SITE = re.compile(
    r'(?i)\b(nyap2p|hhd800|gc2048|zzpp08|javbt|javbus|btt|sis001|sex8|sextb|'
    r'avmoo|javlibrary|dmm|faleno64|kmhp|avsox|nyap2p|guochan2048|big2048|1024dz|'
    r'fengniao\d*|蜂鳥)\b'
)

# 技术标记（含 uncensored/uncen，必须先于站名剥离）
TECH = re.compile(
    r'(?i)(?<![A-Za-z0-9])('
    r'uncensored|uncen|leak|leaked|'
    r'\d{3,4}[pi]|4k|8k|uhd|fhd|qhd|hi10p|10bit|8bit|x26[45]|h26[45]|hevc|avc|vp9|'
    r'\d+fps|blu-?ray|web-?dl|dvdrip|bdrip|no?drm|aac|ac3|'
    r'bluray|bd|hd|sd|wm|ch|uc'
    r')(?![A-Za-z0-9])'
)
# 分辨率词：`1080high` / `1920mid` 这类「数字+画质词」。
# `A_Good_Reason_-_1920low` 剥掉后会露出干净的西方片名（否则会误出 REASON-1920）。
RESWORD = re.compile(r'(?i)(?<![A-Za-z0-9])\d{3,4}(?:low|high|mid)(?![A-Za-z0-9])')
RES_PAIR = re.compile(r'(?i)\b\d{3,4}\s*[x×]\s*\d{3,4}\b')
EXT_TAIL = re.compile(r'(?i)\.(mp4|mkv|avi|wmv|rmvb|webm|ts|mov|flv|m4v|mpg|mpeg)$')


def strip_noise(name):
    """剥掉扩展名 / 括号噪音 / 分辨率 / 技术标记 / 站名，返回干净待匹配串。"""

    s = EXT_TAIL.sub('', name)

    def _keep(m):
        inner = m.group(1)
        if LIKE_NUMBER.search(inner):
            return ' ' + inner + ' '
        return ' '

    s = BRACKET.sub(_keep, s)
    s = RES_PAIR.sub(' ', s)
    s = RESWORD.sub(' ', s)
    s = TECH.sub(' ', s)          # 先剥 uncensored 等，露出干净站名
    s = SITE_NUMDOM.sub(' ', s)
    s = SITE_DOT.sub(' ', s)
    s = SITE_TILDE.sub(' ', s)
    s = BARE_SITE.sub(' ', s)
    # 分隔符归一到**单个空格**。
    # 必要性：Normalizer.clean() 已把 `_`/空格 换成 `-`，叠加 TECH 剥离后会产生
    # `ROSA_HD_00` -> `ROSA--00` 这类连续分隔符，而所有番号形态都是「单个可选
    # 分隔符」，会直接失配（源实现靠 `[_\\.]+ -> ' '` 达到同样效果，这里必须把
    # `-` 一起收进来）。
    # 代价：`Dildo - 179` 这类「西方片名 - 数字」会多出候选 —— 由 RESWORD 剥离
    # 分辨率词 + NOT_STUDIO 词表兜住（全量 26,230 语料复测，多出 7 条，见交付）。
    s = re.sub(r'[-_\\.]+', ' ', s)
    s = re.sub(r'\s+', ' ', s)

    # 统一大写：字典 pattern 不带 (?i)（如 `ABP[-_ ]?\d{3,6}`），
    # 与框架原有行为一致（v2 也是在 match 里做 `text.upper()`）。
    return s.strip().upper()


# ── 三、番号候选 ────────────────────────────────────────────────

P_118 = re.compile(r'(?i)\b118([a-z]{2,5})(\d{3,6})hhb\d?\b')
# FC2 双形态：先 PPV（规范形），再裸数字形。
# 用 `(?<!…)` / `(?!…)` 而非源实现的 `\b` —— 源版在 `FC2PPV-1066192黑暗帝国…`
# 这类「番号紧贴中文」的文件名上会因 CJK 也算 `\w` 而漏掉整条（实测 26,230 语料）。
P_FC2_PPV = re.compile(r'(?i)(?<![A-Za-z0-9])FC2[-_ ]?PPV[-_ ]?(\d{3,9})(?![0-9])')
P_FC2_BARE = re.compile(r'(?i)(?<![A-Za-z0-9])FC2[-_ ]?(\d{3,9})(?![0-9])')
P_STD = re.compile(r'(?i)(?<![A-Za-z0-9])([A-Z]{2,6})[-_ ]?(\d{2,5})(?![0-9])')
P_NOSEP = re.compile(r'(?i)(?<![A-Za-z0-9])([A-Z]{3,6})(\d{2,4})(?![0-9])')
# 字母+数字前缀：T28-571 / T38-123
P_TNUM = re.compile(r'(?i)(?<![A-Za-z0-9])([A-Z]{1,2}\d{1,2})[-_ ](\d{3,5})(?![0-9])')

# 年份形数字：`TASTE-2010` / `BI-2025` / `VDAY-2019` 这类「英文词 + 年份」不是番号
# （全量语料实测 7 例，全部为假阳性）。已知厂牌（KNOWN_STUDIO）不受此限制，
# 避免误伤 `NHDTB-2019` 这种真番号。
YEAR_LIKE = re.compile(r'^(19|20)\d{2}$')
# 数字前缀：300MAAN-403 / 390JNT-022 / 91CM-101
# `(?<![A-Za-z0-9])` 等价源实现的 `\b`，但额外挡住 `_` 前导（框架内 `_` 已归一成 `-`）。
# 关键是**不许从字母数字串中间起匹配**：`C10IDLEA10`、`0gz740xhkob56…` 这类
# 哈希/缓存名会因此吐出 `IDLEA-10`、`XHKOB-56` 假番号。
P_NUMPFX = re.compile(r'(?i)(?<![A-Za-z0-9])\d{2,4}([A-Z]{2,5})[-_ ]?(\d{2,5})(?![0-9])')
# 无厂牌数字番号：FC2 系之外的纯数字（2728927 / 1234567）。
# 只认 7 位 —— 6 位实测全是 `894916` 这类无意义编号，8 位多是日期/ID（见下）。
P_BARE_NUM = re.compile(r'(?<!\d)(\d{6,8})(?!\d)')

# 纯数字候选的合理性闸门（全量语料实测出来的假阳性模式）
DATE_LIKE = re.compile(r'^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$')
NUM_CLUSTER = re.compile(r'\d[-_ ]\d')
HEX_LIKE = re.compile(r'[A-F]')          # 十六进制串里的数字不是番号


def _plausible_bare(clean, match):
    """纯数字无厂牌番号是否可信。

    挡住全量语料实测出来的假阳性：
      * ``HiHSP2202013``        —— 数字是字母串的后缀
      * ``20250629-003709-286`` —— 「日期-时间-序号」串里的数字
      * ``378892d3``            —— 十六进制哈希片段
      * 6 位（``894916``）/ 8 位（``17297746``）—— 实测全是编号、日期或 ID，只认 7 位
    """

    token = match.group(1)

    if len(token) != 7:
        return False
    if DATE_LIKE.match(token):
        return False
    # 前面紧贴字母（如 HiHSP2202013）→ 是长串的一部分
    if match.start() > 0 and clean[match.start() - 1].isalpha():
        return False
    # 后面紧贴字母（如 378892D3）→ 是十六进制/哈希片段
    if match.end() < len(clean) and clean[match.end()].isalpha():
        return False
    # 串里已有「数字-数字」结构 → 是日期/时间/序号串，不是番号
    if NUM_CLUSTER.search(clean):
        return False
    # 串里带 A-F 字母 → 视作哈希/Base32 片段
    if HEX_LIKE.search(clean):
        return False

    return True

NOT_STUDIO = {
    'IMG', 'VID', 'DSC', 'MVI', 'MOV', 'PXL', 'GOPR', 'SCREEN',
    'FUCK', 'SEX', 'MOAN', 'LICK', 'RIDE', 'FACE', 'PART',
    'DISC', 'VOL', 'CLIP', 'FULL', 'SOURCE', 'VIDEO', 'MOVIE',
    'MP', 'FPS', 'BIT', 'FHD', 'UHD', 'HEVC', 'AVC', 'HDR',
    'MP4', 'MKV', 'AVI', 'WMV', 'RMVB', 'WEBM', 'TS', 'HD', 'SD',
    'UC', 'CH', 'WM', 'NEW', 'BEST', 'LONG',
    # 站名残片：`sjhs03.com` / `X2Twitter.com` 剥站名后留下的 COM 会假配成
    # `COM-51`、`COM-2`。TECH 剥离也会把 `WM`/`CH` 之外的 `WWW` 留下。
    'COM', 'WWW',
    # 英文常用词误配：`Strapless Dildo - 179` -> DILDO-179。
    # 词表只放「实测在语料里造成假阳性」的词，不做泛化英语词典。
    'DILDO', 'PORN', 'XXX', 'XXXVIDEO',
}

KNOWN_STUDIO = {
    'ABP', 'ABW', 'ABF', 'ABS', 'SSIS', 'SSNI', 'SONE', 'SOE', 'STARS', 'START',
    'SDDE', 'SDMF', 'SDMU', 'IPX', 'IPZZ', 'IPIT', 'MIDV', 'MIDE', 'MIFD',
    'MIAE', 'MIAA', 'MIAB', 'MIMK', 'PRED', 'DASS', 'DLDSS', 'FSDSS', 'CAWD',
    'EBWH', 'DVAJ', 'SNOS', 'OFJE', 'MKMP', 'DANDY', 'CHN', 'PPT', 'MAAN',
    'JNT', 'HND', 'HNDS', 'HUNTC', 'BBAN', 'NKKD', 'NIMA', 'KNB', 'ASI',
    'NHDTB', 'NHDTC', 'ADN', 'JUFD', 'JUQ', 'JUL', 'MVG', 'HUNBL', 'SACZ',
    'BOBB', 'T28', 'T38', 'GANA', 'HEYZO', 'TOKYO', 'HDKA', 'MDBK', 'SIRO',
    'DVDMS', 'OFJE', 'MIGD', 'NSFS', 'RKI', 'WANZ', 'MXGS', 'MIZD', 'MEYD',
    'URE', 'VEC', 'VRTM', 'GIGL', 'GVH', 'JUKF', 'JJDA', 'JMTY', 'HONB',
    # 2026-09-23 JavDB 实测补录：`261ARA-462` 剥数字前缀后 `ARA-462`
    # 在 JavDB 实存（原形态查不到），属 numpfx 可剥型。
    'ARA',
}

# 通用厂牌规则命中的置信度
CONF_FC2 = 100
CONF_STD_KNOWN = 90
CONF_STD_UNKNOWN = 70
CONF_TNUM = 70
CONF_NUMPFX = 60
CONF_NOSEP = 50
CONF_BARE = 40


def _norm(prefix, num):
    """拼番号。**保留前导零**（与源实现的 lstrip('0') 有意不同，见模块 docstring）。"""

    return '{}-{}'.format(prefix.upper(), num)


def _glued_letter(text, end):
    """`text[end]` 是不是**紧贴**数字的大写字母（番号的续写痕迹）。

    判据用「紧跟其后」而不是「前面有空格」：分隔符已被 `strip_noise` 剥掉，
    `FH 27V` 与 `FH27V` 到这一步都是 `FH27V`。真番号的尾部字母大多另起一段
    （`ABP-171-C` / `ABP-171-U.torrent`），或被 `strip_noise` 当版本标记
    剥掉；只有**连在一起的**才是形态可疑的那种。

    ⚠️ 只在调用方已确认「该粘尾字母不能信」时才用（当前是未收录厂牌）。
    对已收录厂牌不能套 —— `ABP-171UC` 的 UC 是真番号的噪声标记。
    """

    if end >= len(text):

        return False

    return text[end].isalpha() and text[end].isascii()


def _site_padded_num(num):
    """118 系站点 ID -> 番号数字本体。

    `118` 站点把番号数字打成**固定 5 位 ID**，那个零填充属于站点编码，
    不是番号形态。三重实测佐证（2026-09-23）：
      `118abp00171hhb_000^WM.mp4` 在目录 `ABP-171\\` 下，JavDB 收 `ABP-171`
      `[NoDRM]-118abp00108hhb.wmv` 在目录 `[HD]ABP-108\\` 下，JavDB 收 `ABP-108`
      `118ppt00016hhb1.mkv`     在目录 `PPT-016\\` 下，JavDB 收 `PPT-016`
    故剥前导零后按番号规范宽度补足 3 位（<1000 补零，>=1000 原样）。
    """

    value = int(num)

    return '{:03d}'.format(value) if value < 1000 else str(value)


# FC2 前缀形态归一：FC2PPV / FC2_PPV / FC2 PPV -> FC2-PPV
FC2_PREFIX = re.compile(r'^FC2-?PPV(?=[-_]?\d)')

# 无分隔形态：字母前缀直接接数字（ABP00171 / STARS127 / JUL00634）。
# 字典规则的 pattern 里分隔符是可选的（`ABP[-_ ]?\d{3,6}`），命中
# `group(0)` 后**不经过 `_norm()`**，于是同一文件走字典路径得 `ABP00171`、
# 走 P_118/P_STD 得 `ABP-00171` —— 两个键，去重失效。必须在这里兜住。
# **只补分隔符，数字原样**（零填充仍按模块契约保留）。
NO_SEP = re.compile(r'^([A-Z]{2,6})(\d{1,7})$')

# FC2 双形态（canonical 出口用）：补上 PPV 与数字之间的分隔符
FC2_PPV_FORM = re.compile(r'^FC2-?PPV-?(\d{3,9})$')
FC2_BARE_FORM = re.compile(r'^FC2-?(\d{3,9})$')


def canonical(number):
    """番号最终形态归一 —— 全流程唯一的出口整形，保证「稳定、可复现」。

    * 大写
    * ``_`` / 空格 -> ``-``，连续分隔符压成一个 ``-``，去首尾 ``-``
    * 无分隔形态补分隔符：``ABP00171`` -> ``ABP-00171``
    * ``FC2PPV-1234567`` / ``FC2-PPV1234567`` -> ``FC2-PPV-1234567``
    * ``FC2-1234567`` 保持 ``FC2-1234567``（裸数字形，**不补 PPV**）

    **绝不改数字本身** —— 零填充原样保留（``SSIS-001`` 不变成 ``SSIS-1``）。

    补分隔符是**内部一致性**要求，不是审美：字典规则的 pattern 里分隔符
    可选（``ABP[-_ ]?\\d{3,6}``），命中 ``group(0)`` 得 ``ABP00171``；而
    ``P_118`` / ``P_STD`` 那条走 ``_norm()`` 得 ``ABP-00171``。同一个文件
    经两条路径会落成两个键，去重失效、同一部片两条记录。
    """

    number = re.sub(r'[ _]+', '-', (number or '').upper())
    number = re.sub(r'-{2,}', '-', number).strip('-')

    # FC2 家族优先（先于通用无分隔形态，且 FC2 裸号不补 PPV）
    m = FC2_PPV_FORM.match(number)

    if m:
        return 'FC2-PPV-{}'.format(m.group(1))

    m = FC2_BARE_FORM.match(number)

    if m:
        return 'FC2-{}'.format(m.group(1))

    number = FC2_PREFIX.sub('FC2-PPV', number)

    # 通用无分隔形态：ABP00171 -> ABP-00171（数字原样）
    m = NO_SEP.match(number)

    if m:
        return '{}-{}'.format(m.group(1), m.group(2))

    return number


class NumberMatcher:

    def __init__(self, rules):
        self.rules = list(rules or [])

    # ------------------------------------------------------------ 对外入口

    def match(self, text):
        """匹配番号。返回 ``[{number, confidence, source}, ...]``（按置信度降序）。"""

        text = text or ''

        if is_non_jav(text)[0]:
            return []

        clean = strip_noise(text)

        candidates = []

        def add(number, confidence, source):
            number = canonical(number)

            if number:
                candidates.append({
                    "number": number,
                    "confidence": confidence,
                    "source": source,
                })

        # 1) 字典规则（人工策展，priority 直接作为置信度）
        for rule in self.rules:
            pattern = rule.get("pattern")

            if not pattern:
                continue

            for m in re.finditer(pattern, clean):
                # 左边界守卫：字典 pattern 普遍无左边界（`ABP[-_ ]?\d{3,6}`），
                # 会从字母数字串中间起匹配（`XABP123` -> `ABP-123` 假番号）。
                # 引擎规则（P_STD / P_118 …）自带 `(?<![A-Za-z0-9])`，这里补齐。
                # **只挡左边界**：右边界不动，保留「规则写窄 -> 截断」的既有语义
                # （C1b 自愈链路依赖它复现存量错值）。
                if m.start() and clean[m.start() - 1].isascii() \
                        and clean[m.start() - 1].isalnum():
                    continue

                add(
                    m.group(0),
                    rule.get("priority", 50),
                    "dictionary",
                )

        # 2) 通用引擎（移植自 code_extract3）
        # 先记下 118 系匹配覆盖的区间：`118abp00171hhb` 是**一个**站点 token，
        # 其中的 `118` + 字母 + 数字会被 P_NUMPFX 再匹配一次，产出同义异形键
        # （ABP-171 vs ABP-00171）→ 去重失效 → 同一部片两条记录。
        # 实测：`118abp00171hhb.mp4` 在修 118 零填充之前两条规则恰好都吐
        # `ABP-00171`（靠巧合去重）；修完之后分歧才显形。故显式排除重叠区间。
        span_118 = []

        for m in P_118.finditer(clean):
            # 118 站点的 5 位零填充是站点 ID 编码，须还原成番号本体数字
            # （`118abp00171hhb` -> ABP-171，见 _site_padded_num 的三重佐证）
            add(
                _norm(m.group(1), _site_padded_num(m.group(2))),
                CONF_STD_KNOWN,
                '118',
            )

            span_118.append(
                (m.start(), m.end())
            )

        def _in_118(m):
            """该匹配是否落在 118 站点 token 内。"""

            return any(
                start <= m.start() and m.end() <= end
                for start, end in span_118
            )

        for m in P_FC2_PPV.finditer(clean):
            add('FC2-PPV-{}'.format(m.group(1)), CONF_FC2, 'fc2')

        for m in P_FC2_BARE.finditer(clean):
            add('FC2-{}'.format(m.group(1)), CONF_FC2, 'fc2')

        for m in P_STD.finditer(clean):
            prefix = m.group(1).upper()

            if prefix in NOT_STUDIO:
                continue
            # 未收录厂牌 + 年份形数字 -> 假阳性（TASTE-2010 / BI-2025 / VDAY-2019）
            if YEAR_LIKE.match(m.group(2)) and prefix not in KNOWN_STUDIO:
                continue

            # 数字后面**直接粘着**字母时，未收录厂牌 + **2 位数字**不认
            # （2026-09-24）。
            #
            # 实测（主人报的 FH-27）：目录名 `…激情啪啪等FH 27V` 里的 `27V`
            # 被抠成 `FH-27`（尾部 V 被当版本标记丢掉），而本地其实是 25 集
            # 自拍短片 —— 于是 JavDB 上 FH-27 的磁力与评论被强加在错误资源上。
            #
            # ⚠️ 为什么必须卡「2 位数字」这一条 —— 我第一版没卡，把真分片
            # 一起挡了（实测 `DSVR-219D.VR.mp4` / `KSDO-021A.avi` 当场认不出，
            # 重扫会让它们掉出库，直接抵消「保卡」）。语料实测分布：
            #     2 位数字 + 字母 -> **0 条**（从没有真实番号长这样）
            #     3-5 位数字 + 字母 -> 51 条，全是分片标记
            #         （ATID-516C / OFJE-312A / MVSD-513C / FSDSS-274ch …）
            # 所以「2 位 + 粘字母」是**纯噪声形态**，卡在这里既准又零误伤。
            if prefix not in KNOWN_STUDIO \
                    and len(m.group(2)) <= 2 \
                    and _glued_letter(clean, m.end()):
                continue

            add(
                _norm(prefix, m.group(2)),
                CONF_STD_KNOWN if prefix in KNOWN_STUDIO else CONF_STD_UNKNOWN,
                'std',
            )

        for m in P_TNUM.finditer(clean):
            add(_norm(m.group(1), m.group(2)), CONF_TNUM, 'tnum')

        for m in P_NUMPFX.finditer(clean):
            prefix = m.group(1).upper()

            if prefix in NOT_STUDIO:
                continue

            # 落在 118 站点 token 内的不再重复产出（同义异形键，见上）
            if _in_118(m):
                continue

            # numpfx 置信度按厂牌分档（2026-09-23 JavDB 实测校准）：
            #   `300MAAN-403`->MAAN-403 / `200GANA-3309`->GANA-3309 /
            #   `390JNT-022`->JNT-022 / `261ARA-462`->ARA-462 —— 剥掉数字
            #   前缀后在 JavDB 均**实存**，而原形态一律查不到；说明这里的
            #   数字是发布方打的系列标，剥掉才是真番号。
            #   一律给 60 会把真番号压在可信线以下 → 剥后前缀已收录则 90。
            #   未收录前缀（如 `91CM-101`->CM-101）维持 60：JavDB 对
            #   91CM 全形态（91CM-101/182/190）**均无覆盖**，无 ground
            #   truth 可判定剥与不剥孰对，故沿用既有形态、不改判。
            add(
                _norm(prefix, m.group(2)),
                CONF_STD_KNOWN if prefix in KNOWN_STUDIO else CONF_NUMPFX,
                'numpfx',
            )

        # 3) 兜底：只在前面全空时才跑（源实现同序，避免把 FC2 的数字重复计一遍）
        if not candidates:
            for m in P_NOSEP.finditer(clean):
                prefix = m.group(1).upper()

                if prefix in NOT_STUDIO:
                    continue
                # 年份形数字同样要挡 —— 否则 `VDAY2019` 会被 P_STD 挡掉后
                # 从这里重新放进来（兜底绕过过滤器，实测 3 条真实假阳性）。
                if YEAR_LIKE.match(m.group(2)) and prefix not in KNOWN_STUDIO:
                    continue

                add(_norm(prefix, m.group(2)), CONF_NOSEP, 'nosep')

            for m in P_BARE_NUM.finditer(clean):
                if _plausible_bare(clean, m):
                    add(m.group(1), CONF_BARE, 'bare')

        return self.unique(candidates)

    # ------------------------------------------------------------ 工具

    def unique(self, items):
        """按 number 去重，同号保留**置信度最高**的一条，按置信度降序返回。

        降序是 ``match()`` docstring 承诺的契约（D2 去重层依赖"第一个最可信"），
        原实现返回首次出现顺序 —— 与承诺不符。实测不影响 ``best_number()``
        （它用 ``max()``），但契约必须自洽。
        """

        best = {}

        for item in items:
            number = item["number"]

            if number not in best or item["confidence"] > best[number]["confidence"]:
                best[number] = item

        return sorted(
            best.values(),
            key=lambda item: item["confidence"],
            reverse=True,
        )
