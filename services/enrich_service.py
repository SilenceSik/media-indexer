"""元数据抓取服务：番号 -> JavDB 元数据 + 磁力 + 封面 + 内容截图。

**只在用户显式触发时运行**（Web 上的按钮 / CLI 脚本），不做扫描后自动抓取 ——
自动抓取会在用户没要求时联网、耗时且难以预期。

落库一律走 core.database_v2.Database，不自己拼 SQL。
"""

import os

from adapters.javdb_adapter import JavDBCLIClient

from core.magnet_judge import (
    judge,
    normalize_number,
    tier_of,
)

from core.duration_check import mismatch_verdict, number_in_filename


def resolve_previews_dir():
    """宣传视频落盘根目录。不动 config.yaml 的前提下给个稳的默认值。"""

    env = os.environ.get("LMM_PREVIEWS")

    if env:

        return os.path.abspath(env)

    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "storage", "previews")
    )


class EnrichService:

    def __init__(
        self,
        db,
        covers_dir,
        screenshots_dir,
        client=None,
        fallback_client=None,
        previews_dir=None
    ):

        self.db = db

        self.covers_dir = covers_dir

        self.screenshots_dir = screenshots_dir

        self.client = client or JavDBCLIClient()

        # 兜底源（JavBus）。**只在主源查不到时用**，且必须显式传入 ——
        # 不在这里默认构造，避免测试/离线场景意外联网。
        self.fallback_client = fallback_client

        # 宣传视频落盘目录（每番号一个子目录）。
        self.previews_dir = previews_dir or resolve_previews_dir()

    # ------------------------------------------------------------ 宣传视频

    def save_preview_video(self, number, url=None):
        """把宣传视频存成**能直接播的 mp4**，返回 `{ok, file, error}`。

        ## 为什么不能只存链接

        javdb 给的是 **HLS 播放列表**（`.m3u8`），而且分片是 AES-128 加密的。
        浏览器原生**不支持** HLS，Chrome/Edge 拿到只会下载、不会播 ——
        主人 2026-09-24 点「宣传视频」时看到的正是「直接转下载了」。

        所以这里转成 mp4 落盘（`<previews>/<番号>/preview.mp4`），
        由 web 层当普通视频喂给 `<video>`。**比引 hls.js 轻**：
        录一次存本地，之后离线可播、也不依赖那个会过期的签名链接。

        ## 为什么走官方 CLI 而不是自己调 ffmpeg

        `javdb assets download -o X.mp4` 本来就是干这个的（官方实现，
        含 HLS + AES 处理），比在项目里再搓一条 ffmpeg 管线干净，
        也少一个对外部二进制的依赖。实测同一链接两边都能出片。

        ## 歧义番号

        `assets list` 对歧义番号会失败（与 assets 的既有坑一致），
        所以 URL 优先用**存库的那条**；库里没有才回退去问 CLI。
        """

        # 番号存在 titles 里，链接存在 metadata 里 —— **分两处查**。
        # 早前一句 JOIN 就把「没抓过元数据（metadata 无行）」误报成
        # 「库里没有该番号」；而调用方明明把 URL 传进来了。
        title = self.db.conn.execute(
            "SELECT id FROM titles WHERE number = ?", (number,)
        ).fetchone()

        if not title:

            return {"ok": False, "file": None, "error": "库里没有该番号"}

        title_id = title[0]

        if not url:

            m = self.db.conn.execute(
                "SELECT preview_video FROM metadata WHERE title_id = ?",
                (title_id,),
            ).fetchone()

            url = m[0] if m else None

        if not url:
            return {"ok": False, "file": None,
                    "error": "还没抓过宣传视频链接，先点「抓宣传视频」"}

        directory = os.path.join(self.previews_dir, number)

        os.makedirs(directory, exist_ok=True)

        target = os.path.join(directory, "preview.mp4")

        # 已存在且非空 -> 不重复下载（签名链接会过期，但本地文件不会）
        if os.path.exists(target) and os.path.getsize(target) > 0:

            return {"ok": True, "file": target, "cached": True}

        client = self.client

        try:

            got = client.download_assets(
                ["video\t{}".format(url)], directory,
                # 封面/截图的落盘名由 CLI 决定，视频要固定名才好在模板里引用，
                # 所以下完再改名（下载目标不能已存在，CLI 有意这么设计）。
                filename="preview.mp4",
            )

        except TypeError:

            # client 不支持 filename 参数（老签名/测试替身）-> 退回默认名
            got = client.download_assets(["video\t{}".format(url)], directory)

        if not got:

            return {"ok": False, "file": None, "error": "下载失败（链接可能已过期）"}

        produced = got[0]

        if os.path.abspath(produced) != os.path.abspath(target):

            if os.path.exists(target):
                os.remove(target)

            os.replace(produced, target)

        if not os.path.exists(target) or os.path.getsize(target) == 0:

            return {"ok": False, "file": None, "error": "下载结果为空"}

        return {"ok": True, "file": target}

    # ------------------------------------------------------------ 对外

    def enrich(self, number, want="all"):
        """抓取并落库一个番号。

        want: "all"     -> 元数据 + 磁力 + 封面 + 截图
              "magnets" -> 只抓磁力（+ 元数据，因为 detail 一次返回）
        """

        result = {
            "number": number,
            "metadata": False,
            "magnets": 0,
            "cover": None,
            "screenshots": 0,
            "ambiguous_versions": None,
            "error": None,
            # 时长完全不符（本地这批文件不是这个番号）—— 默认否，
            # 判出来才置 True 并触发剔除。见下面 `_judge_and_store` 之后。
            "duration_mismatch": False,
        }

        # ── 主源查询（需要拿到「确定查不到」还是「请求失败」）──
        #
        # 不能用 `detail()` —— 它在两种情况下都返回 None，区分不出来。
        if hasattr(self.client, "detail_checked"):

            detail, lookup_status = self.client.detail_checked(number)

        else:

            detail = self.client.detail(number)

            lookup_status = "ok" if detail else "notfound"

        source = "javdb"

        # ── 兜底：主源查不到时试 JavBus ──
        #
        # 只在**主源明确查不到**时才走，不覆盖主源的任何结果。
        if not detail and lookup_status == "notfound" \
                and self.fallback_client is not None:

            try:

                detail = self.fallback_client.detail(number)

                if detail:
                    source = "javbus"

            except Exception as exc:                            # noqa: BLE001

                # 兜底自己炸了不能影响主流程的结论，但也**不能因此判定
                # 番号不存在** —— 标成 error 让剔除逻辑跳过
                result["fallback_error"] = f"{type(exc).__name__}: {exc}"

                lookup_status = "error:fallback"

        result["source"] = source if detail else None

        if not detail:

            result["error"] = "javdb 与 javbus 都查不到该番号"

            result["lookup_status"] = lookup_status

            # ── D8：**确定**查不到 -> 从媒体库剔除 ──
            #
            # 之前只在这里返回 error，**记录仍留在库里** —— 结果那 49 个
            # 查不到的番号（AMQ-13 / CUM-60 / DVDRIP-1024 …）照样出现在
            # 首页和卡片上，与「查不到的不进媒体库」这条规则不符。
            #
            # ⚠️ 只有 `notfound` 才剔。网络故障/超时（`error:*`）绝不剔 ——
            # 一次抖动就会把真番号清掉。
            result["removed"] = self._drop_not_found(number, lookup_status)

            return result

        # 番号对应多个影片 ID 时，适配器会把各版本磁力合并后返回
        result["ambiguous_versions"] = detail.get("ambiguous_versions")

        # ── 素材：封面 + 截图（want=all 才抓）──
        #
        # 独立 try：素材失败不能让整条记录变成「查不到」。
        # 早前把素材下载放在 detail 的 try 里，素材一报错整条就夭折，
        # 磁力明明拿到了也落不了库。
        cover_local = None
        screenshot_names = []

        if want == "all":

            try:

                cover_local, screenshot_names = self.fetch_media(number)

            except Exception as exc:                            # noqa: BLE001

                # 素材失败不覆盖已算作成功的元数据/磁力，只记一条提示
                result["media_error"] = f"{type(exc).__name__}: {exc}"

        # ── 元数据 ──
        acts = detail.get("actors") or []

        tags = detail.get("tags") or []

        self.db.save_metadata(
            {
                "number": number,
                "title": detail.get("title") or detail.get("origin_title"),
                "cover": detail.get("cover_url"),
                "cover_local": cover_local,
                "release_date": detail.get("release_date"),
                "maker": detail.get("maker_name"),
                "actresses": acts,
                "tags": tags,
                "screenshots": screenshot_names,
            }
        )

        result["metadata"] = True

        result["cover"] = cover_local

        result["screenshots"] = len(screenshot_names)

        # ── 磁力 ──
        magnets = detail.get("magnets")

        if not magnets:

            magnets = detail.get("magnets_list") or []

        if isinstance(magnets, list):

            for m in magnets:

                if not isinstance(m, dict):

                    continue

                uri = self.magnet_uri(m)

                if not uri:

                    continue

                size_text = self.size_text(m.get("size"))

                self.db.add_magnet(
                    number,
                    uri,
                    source=m.get("name") or "",
                    size_text=size_text,
                    # ⚠️ 不再是 1。旧代码把所有拿到的磁力一律写成 verified=1，
                    # 门控只要求「至少 1 条」—— 1 条挂错的磁力就能放行删除
                    # （P0-1）。现在 verified 的语义收紧为「这条确认属于该番号」，
                    # 由下面的判定器算出来再回填。
                    verified=0,
                    commit=False,
                )

                result["magnets"] += 1

            self.db.conn.commit()

        # ── 判定 + 定档（D9/D11 的落地处）──
        #
        # 位置很关键：必须在磁力写完之后，因为判定器要按**实际入库的**磁力算。
        # 这一步同时解决两个 P0：
        #   P0-1 门控规则不对 -> verified 改由判定结果决定
        #   P0-2 不核对返回的 number -> 在下面比对，不一致记 mismatch
        #
        # ⚠️ **没有磁力也要判**。早前这里有个 `and magnets` 的条件，导致
        # 「查到了番号但一条磁力都没有」的条目**档位一直是 NULL** ——
        # 而 D11 明确说这种是「极低置信」。NULL 与「极低」在界面上
        # 表现完全不同：NULL 看起来像「没抓过」，用户会以为任务漏了。
        # 实测 KW-7142 就是这样（有元数据、0 磁力、tier 空）。
        #
        # `_judge_and_store` 顺带把时长取回来（它已经查过一遍文件了，
        # 不再重复扫），这里据它做判定。
        dur = self._judge_and_store(number, detail, magnets or [], source)

        pairs = dur["pairs"]
        fragments = dur["fragments"]
        partial_ok = dur["partial_ok"]

        # ── 时长判定：完全不符就扣到 0 并踢出媒体库 ──
        #
        # 主人 2026-09-24 定：像 FH-27 那种「9 分钟 vs 130 分钟」的超大差距
        # 不只扣分，**要在抓元数据时把它从媒体库踢出去**。
        # 小偏差（广告片头、预览片段）仍然只提示不拒绝 ——
        # 见 `duration_check` 里的两段式说明与分片豁免（KSDO-021 那类
        # 分片真片单看每文件都只有 19%，一刀切会误杀）。
        verdict = mismatch_verdict(pairs, partial_ok=partial_ok) if pairs else None

        if verdict and verdict["mismatch"]:

            result["duration_verdict"] = verdict["reason"]

            result["removed"] = self._drop_duration_mismatch(number, verdict)

            result["duration_mismatch"] = True

            result["error"] = (
                "本地文件与元数据不是同一部片（时长完全不符），"
                "已从媒体库剔除"
            )

        else:

            # 没被判完全不符，才记「疑似片段」的提示 ——
            # 否则「仅提示、未拦截」与随后的剔除自相矛盾。
            if fragments:

                self._log_fragment_hints(number, fragments)

            if verdict:

                result["duration_verdict"] = verdict["reason"]

        return result

    def _judge_and_store(self, number, detail, magnets, source="javdb"):
        """把判定结果落到 titles / magnets（D9 分档 + D11 的 number 核对）。

        产出全部写库，前端与删除门控都读库，不各自重算。

        ``source`` 是这条元数据从哪来的（`javdb` / `javbus`），落库后
        能看出兜底有没有被用上。
        """

        res = judge(magnets, number)

        # ── P0-2：核对 javdb 返回的番号 ──
        #
        # 搜错或识别错时，会把**别的片**的磁力、封面挂到这个文件上，
        # 还会被错误地定成高档。这条同时是 D11 批量删的第二个条件。
        #
        # 注意：detail 里还有 actor_movies / relative_movies 等其他番号，
        # 只认最外层的 number。
        returned = (detail.get("number") or "").strip()

        matched = bool(returned) and (
            normalize_number(returned) == normalize_number(number)
        )

        comments = (
            detail.get("comments_count")
            if detail.get("comments_count") is not None
            else detail.get("reviews_count")
        )

        tier = tier_of(res["correct"], comments)

        title_id = self.db.get_or_create_title(number)

        self.db.save_tier_evidence(title_id, {
            "number": number,
            "correct": res["correct"],
            "comments": comments,
            "tier": tier,
            "javdb_number": returned,
            "matches": matched,
            "source": source,
            "magnets": [],
        })

        # 逐条回填 verified / is_correct。
        #
        # 一次建 name -> uri 的索引再回填，别在循环里线性扫 magnets
        # （判定结果与 magnets 一一对应，靠名字配回来最直接）。
        uri_by_name = {}

        for m in magnets:

            if not isinstance(m, dict):
                continue

            uri = self.magnet_uri(m)

            if uri:
                uri_by_name.setdefault(m.get("name") or "", uri)

        for row in res["magnets"]:

            uri = uri_by_name.get(row["name"])

            if not uri:
                continue

            # verified 的语义收紧为「这条确认属于该番号」—— 旧门控
            # `EXISTS verified=1` 因此自动变正确（向后兼容），
            # 新门控 deletion_eligibility() 再叠 D9/D11 两层。
            self.db.conn.execute(
                """
                UPDATE magnets
                SET verified = ?, is_correct = ?, magnet_hash = ?, name = ?
                WHERE title_id = ? AND magnet = ?
                """,
                (
                    1 if row["is_correct"] else 0,
                    1 if row["is_correct"] else 0,
                    row["hash"],
                    row["name"],
                    title_id,
                    uri,
                ),
            )

        self.db.conn.commit()

        # ── 时长比对（取数落库）──
        #
        # 只负责取数与落库，**判不判由调用方决定** —— 这里拿不到
        # `result`，扣分/剔除要写进 enrich 的返回值，所以在上面做。
        pairs, fragments = self._check_duration(title_id, number, detail)

        # 番号是否写在**文件名**里 —— 决定「认错片」还是「内容没下全」。
        # 见 core/duration_check.number_in_filename 的说明。
        alive_paths = [
            r[0] for r in self.db.conn.execute(
                "SELECT filepath FROM media_files "
                "WHERE title_id = ? AND COALESCE(local_deleted, 0) = 0",
                (title_id,),
            ).fetchall()
        ]

        # 两个条件缺一不可：番号在文件名里 **且** 是多个文件。
        # 只满足前者会把 `IPX-951`（单片自拍合集）也放过（实测）。
        partial_ok = len(alive_paths) >= 2 and all(
            number_in_filename(number, p) for p in alive_paths
        )

        return {"pairs": pairs, "fragments": fragments,
                "partial_ok": partial_ok}

    def _check_duration(self, title_id, number, detail):
        """本地文件时长 vs 元数据时长。记录并返回判定所需的原始数据。

        主人 2026-09-24 定（两段式）：
          * **小偏差**（几分钟广告片头、长花絮）-> 只评分、只提示，不拒绝
            （真片会被误杀，前面 `duration_check` 的模块说明有实测）
          * **超大差距**（本地与元数据根本不是同一部片）-> 扣到 0 并踢出库

        本条只负责**取数与落库**，判不判由调用方拿返回的列表去问
        `duration_check.mismatch_verdict`。

        返回 `(pairs, fragments)`：
          * `pairs`     `[(本地秒, 元数据秒), ...]`，本次**成功探到的**文件
          * `fragments` 疑似片段的提示行 `[(path, filename, 秒, 元数据分)]`

        **不提 filter_log**：提示文案里写着「仅提示、未拦截」，而调用方
        随后可能真的拦截（判为完全不符）—— 那就成了假话。所以这里只把
        候选带回去，由调用方在**知道结论之后**决定记哪条。
        """

        from core.duration_check import (
            compare,
            is_likely_fragment,
            probe_duration,
        )

        ref_minutes = detail.get("duration")

        if ref_minutes is None:
            ref_minutes = detail.get("video_length")

        if not ref_minutes:
            return None, []

        rows = self.db.conn.execute(
            # 只探**还在磁盘上的**文件：已删的没有可探的，且它们的旧时长
            # 不该再参与「本地是否就是这部片」的判定。
            "SELECT id, filepath FROM media_files "
            "WHERE title_id = ? AND COALESCE(local_deleted, 0) = 0",
            (title_id,),
        ).fetchall()

        pairs = []

        fragments = []

        for fid, path in rows:

            if not path or not os.path.exists(path):
                continue

            secs = probe_duration(path)

            if secs is None:
                continue

            c = compare(secs, ref_minutes)

            pairs.append((secs, float(ref_minutes) * 60.0))

            self.db.conn.execute(
                """
                UPDATE media_files
                SET duration_local = ?, duration_ref = ?, duration_ratio = ?
                WHERE id = ?
                """,
                (secs, float(ref_minutes), c["ratio"] if c else None, fid),
            )

            if is_likely_fragment(secs, ref_minutes):

                fragments.append(
                    (path, os.path.basename(path), secs, float(ref_minutes))
                )

        self.db.conn.commit()

        return (pairs or None), fragments

    def _log_fragment_hints(self, number, fragments):
        """疑似片段提示（**只在没被判「完全不符」时记**）。

        一旦判为完全不符，这些文件会走 `duration_mismatch` 那条记录，
        再记一条「仅提示、未拦截」就是自相矛盾了。
        """

        from core import filter_log

        for path, filename, secs, ref_minutes in fragments:

            filter_log.record(
                self.db,
                path=path,
                filename=filename,
                number=number,
                reason="duration_off",
                detail=(
                    "本地 {:.1f} 分钟 vs 元数据 {:.0f} 分钟 —— "
                    "疑似片段/预览（仅提示，未拦截）".format(
                        secs / 60.0, ref_minutes)
                ),
                stage="duration",
            )

        filter_log.flush(self.db)

    def _drop_duration_mismatch(self, number, verdict):
        """时长完全不符 -> 从**媒体库**剔除（磁盘文件一个不动）。

        与 `_drop_not_found` 是同族的库记录清理，但**原因完全不同**，
        所以原因码分开记：
          * `lookup_notfound`    站上**没有**这个番号
          * `duration_mismatch`  站上有，但本地这些文件**是另一部片**

        混记会让「查不到」的统计说谎（那是判断两类问题的依据）。
        """

        from core import filter_log

        rows = self.db.conn.execute(
            "SELECT id, filepath, filename, size FROM media_files "
            "WHERE title_id IN (SELECT id FROM titles WHERE number = ?)",
            (number,),
        ).fetchall()

        if not rows:
            return 0

        med = verdict.get("median")

        detail = (
            "本地时长与元数据差得太远（每文件比例中位数 {:.2f}，"
            "且求和比 {:.2f} 排除了分片）—— 本地这批文件并非 {}。"
            "已从媒体库剔除，磁盘文件未动。".format(
                med if med is not None else -1,
                verdict.get("sum_ratio") if verdict.get("sum_ratio") is not None
                else -1,
                number,
            )
        )

        for _fid, filepath, filename, size in rows:

            filter_log.record(
                self.db,
                path=filepath,
                filename=filename,
                size=size,
                number=number,
                reason="duration_mismatch",
                detail=detail,
                stage="duration",
            )

        title_id = self.db.conn.execute(
            "SELECT id FROM titles WHERE number = ?", (number,)
        ).fetchone()[0]

        for sql in (
            "DELETE FROM comments WHERE title_id = ?",
            "DELETE FROM magnets WHERE title_id = ?",
            "DELETE FROM metadata WHERE title_id = ?",
            "DELETE FROM media_files WHERE title_id = ?",
            "DELETE FROM titles WHERE id = ?",
        ):
            self.db.conn.execute(sql, (title_id,))

        filter_log.flush(self.db)
        self.db.conn.commit()

        return len(rows)

    def _drop_not_found(self, number, lookup_status):
        """按 D8 把「确定查不到」的番号从媒体库剔除。

        只做**库记录**的移除 —— 磁盘文件一个不动。这是 D8「查不到的不进
        媒体库」，不是删除文件。

        三件事缺一不可：
          1. 把关联文件写进筛选日志（**留痕**，否则用户不知道少了什么）
          2. 删 media_files / metadata / magnets（留在库里会挂死关联）
          3. 删 titles

        返回被剔除的文件数；`lookup_status != "notfound"` 时**什么都不做**
        并返回 0 —— 网络故障绝不能触发剔除。
        """

        if lookup_status != "notfound":

            return 0

        from core import filter_log

        rows = self.db.conn.execute(
            "SELECT id, filepath, filename, size FROM media_files "
            "WHERE title_id IN (SELECT id FROM titles WHERE number = ?)",
            (number,),
        ).fetchall()

        if not rows:

            return 0

        for fid, filepath, filename, size in rows:

            filter_log.record(
                self.db,
                path=filepath,
                filename=filename,
                size=size,
                number=number,
                reason="lookup_notfound",
                detail=(
                    "javdb 与 javbus 都查不到该番号（D8 闸门）—— "
                    "已从媒体库剔除，磁盘文件未动"
                ),
                stage="lookup",
            )

        self.db.conn.execute(
            "DELETE FROM magnets WHERE title_id IN "
            "(SELECT id FROM titles WHERE number = ?)",
            (number,),
        )

        self.db.conn.execute(
            "DELETE FROM metadata WHERE title_id IN "
            "(SELECT id FROM titles WHERE number = ?)",
            (number,),
        )

        n = self.db.conn.execute(
            "DELETE FROM media_files WHERE title_id IN "
            "(SELECT id FROM titles WHERE number = ?)",
            (number,),
        ).rowcount

        self.db.conn.execute(
            "DELETE FROM titles WHERE number = ?",
            (number,),
        )

        self.db.conn.commit()

        return n

    # ------------------------------------------------------------ 工具

    @staticmethod
    def magnet_uri(m):
        """从 JavDB 的磁力对象拼 magnet URI。

        优先用现成的 magnet 字段；否则用 hash 拼（含 dn 名称）。
        """

        uri = m.get("magnet") or m.get("link")

        if uri:

            return uri

        h = m.get("hash")

        if not h:

            return None

        uri = "magnet:?xt=urn:btih:" + str(h)

        if m.get("name"):

            uri += "&dn=" + str(m["name"])

        return uri

    @staticmethod
    def size_text(size_mb):
        """JavDB 的 size 单位是 **MB**（实测 5880 -> 5.74 GB），不是字节。"""

        if not size_mb:

            return ""

        try:

            value = float(size_mb)

        except (TypeError, ValueError):

            return str(size_mb)

        if value >= 1024:

            return f"{value / 1024:.2f} GB"

        return f"{value:.0f} MB"

    def fetch_media(self, number):
        """下载封面 + 内容截图，返回 (封面文件名, 截图文件名列表)。

        官方管道 `assets list --type image` 给三类：
          small_cover / cover / samples（正片截图，约 10 张）
        前 2 条当封面，其余 /samples/ 当截图。
        """

        assets = self.client.assets(number, "image")

        if not assets:

            return None, []

        urls = [f"{t}\t{u}" for t, u in assets]

        cover_lines = urls[:2]

        shot_lines = [l for l in urls[2:] if "/samples/" in l]

        cover_name = None

        shot_names = []

        cover_dir = os.path.join(self.covers_dir, number)

        if cover_lines and not self._has_files(cover_dir):

            got = self.client.download_assets(cover_lines, cover_dir)

            if got:

                got.sort(
                    key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0,
                    reverse=True,
                )

                cover_name = self._relpath(got[0], self.covers_dir)

        elif cover_lines:

            files = self._files(cover_dir)

            if files:

                files.sort(
                    key=lambda p: os.path.getsize(p),
                    reverse=True,
                )

                cover_name = self._relpath(files[0], self.covers_dir)

        shot_dir = os.path.join(self.screenshots_dir, number)

        if shot_lines and not self._has_files(shot_dir):

            got = self.client.download_assets(shot_lines, shot_dir)

            shot_names = [self._relpath(p, self.screenshots_dir) for p in got]

        elif shot_lines:

            shot_names = [
                self._relpath(p, self.screenshots_dir)
                for p in self._files(shot_dir)
            ]

        return cover_name, shot_names

    @staticmethod
    def _relpath(path, root):
        """落盘路径 -> 相对根目录的路径（正斜杠）。

        **必须存相对路径而不是 basename**：落盘结构是
        `<root>/<番号>/image-002.jpg`，各番号的文件名完全一样
        （都叫 image-002.jpg）。只存 basename 会让全站封面撞成同一个名字。
        """

        rel = os.path.relpath(
            os.path.abspath(path),
            os.path.abspath(root),
        )

        return rel.replace("\\", "/")

    @staticmethod
    def _files(directory):

        if not os.path.isdir(directory):

            return []

        return [
            os.path.join(directory, n)
            for n in sorted(os.listdir(directory))
            if os.path.isfile(os.path.join(directory, n))
        ]

    @staticmethod
    def _has_files(directory):

        return bool(EnrichService._files(directory))
