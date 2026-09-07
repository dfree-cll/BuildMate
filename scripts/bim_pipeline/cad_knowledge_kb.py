# -*- coding: utf-8 -*-
"""图纸解析知识库(cad_knowledge)访问模块——"四本账":
1. layer_map.json        图层语义映射账本(见过哪些图层名→语义角色)
2. pitfalls.json         坑位自查清单(解析完自动跑一遍)
3. project_profiles.json 项目画像账本(每项目: 图层/块/参数/质量分)
4. (运行中) 新项目自动入库 → 换项目先查库再探测

用法:
    kb = KnowledgeBase()
    kb.resolve_layers(all_layers)   # 查库+关键词探测 → 确定 柱/轴网/墙 图层
    kb.check_pitfalls(...)          # 坑位自查
    kb.save_profile(...)            # 项目画像入库
"""
import json
import os
import glob
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.environ.get('CAD_KNOWLEDGE_DIR', os.path.join(
    PROJECT_ROOT, 'data', 'runtime', 'cad_knowledge'))
LAYER_MAP = os.path.join(KB_DIR, 'layer_map.json')
PITFALLS = os.path.join(KB_DIR, 'pitfalls.json')
PROFILES = os.path.join(KB_DIR, 'project_profiles.json')


class KnowledgeBase:
    def __init__(self):
        self.layer_map = self._load(LAYER_MAP, {})
        self.pitfalls = self._load(PITFALLS, {})
        self.profiles = self._load(PROFILES, {})

    def _load(self, path, default):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default

    def _save(self, path, data):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 图层解析: 查库 → 关键词探测 → 统计兜底 ----------
    def get_layer_kw(self, role):
        """从 layer_map 读角色关键词列表(如 '轴网线图层' → [S-ANNO-DOTE, S-ANNO-AXIS, ...])
        这是"入库即生效"的关键: 提取器从 KB 读图层关键词, 不写死
        """
        v = self.layer_map.get(role)
        if isinstance(v, list):
            # 去掉括号注释 (如 'S-ANNO-DOTE(点划线)' → 'S-ANNO-DOTE')
            return [k.split('(')[0].strip() for k in v if k]
        if isinstance(v, str):
            return [v]
        return []

    def resolve_layers(self, all_layer_names, layer_entity_types=None):
        """输入: 图中全部图层名列表。输出: {'柱': 'xxx', '轴网': 'xxx', '墙': 'xxx', ...}
        优先级: ①项目画像里查到的同风格图层(需通过实体类型验证) ②layer_map 关键词 ③统计兜底
        layer_entity_types: {图层名: {实体类型: 数量}}——轴网必须含 LINE, 纯文字层(A-AXIS-TXT)不算
        """
        result = {}
        layer_map = self.layer_map.get('语义角色', {})
        ent = layer_entity_types or {}

        def _verify(role, layer):
            """验证图层是否真的是该角色: 轴网须含 LINE, 柱须含 LWPOLYLINE/POLYLINE/INSERT"""
            types = ent.get(layer, {})
            if role == '轴网':
                return (types.get('LINE', 0) + types.get('LWPOLYLINE', 0)) >= 5  # LINE 或 LWPOLYLINE 都算
            if role == '柱':
                return (types.get('LWPOLYLINE', 0) + types.get('POLYLINE', 0) + types.get('INSERT', 0)) > 0
            return True

        # ① 项目画像优先(需验证)
        cand = self._match_profile(all_layer_names)
        if cand:
            lm = cand.get('图层映射', {})
            for role, known in lm.items():
                if role in result:
                    continue
                kws = known if isinstance(known, list) else [known]
                for k in kws:
                    hit = [l for l in all_layer_names if k and k in l]
                    if hit:
                        hit.sort(key=len)
                        for h in hit:
                            if _verify(role, h):
                                result[role] = h
                                break
                        if role in result:
                            break

        # ② layer_map 关键词补漏(也验证)
        for role, cfg in layer_map.items():
            if role in result:
                continue
            for kw in cfg.get('关键词', []):
                hit = [l for l in all_layer_names if kw.lower() in l.lower()]
                if hit:
                    hit.sort(key=len)
                    for h in hit:
                        if _verify(role, h):
                            result[role] = h
                            break
                    if role in result:
                        break
        return result

    def _match_profile(self, all_layer_names):
        """按图层名重合度找历史项目——同设计院复用率最高"""
        best, best_score = None, 0
        for proj in self.profiles.get('项目', []):
            lm = proj.get('图层映射', {})
            score = 0
            for role, known in lm.items():
                kws = known if isinstance(known, list) else [known]
                for k in kws:
                    if k and any(k in l for l in all_layer_names):
                        score += 1
            if score > best_score:
                best, best_score = proj, score
        return best if best_score > 0 else None

    # ---------- 坑位自查 ----------
    def check_pitfalls(self, report):
        """report: dict, 解析结果摘要。命中坑位返回 [(坑id, 名称, 数据)], 否则 []"""
        hits = []
        for pit in self.pitfalls.get('坑位', []):
            pid = pit['id']
            # 每个坑一个自查函数（阈值已按 G40-01 校准——成长机制）
            if pid == 'PIT-01' and report.get('axis_count') and report.get('axis_label_count'):
                if report['axis_count'] > report['axis_label_count']:
                    hits.append((pid, pit['名称'], '轴网%d > 轴号%d' % (report['axis_count'], report['axis_label_count'])))
            elif pid == 'PIT-03' and report.get('orient_flips'):
                # 校准: 非方柱数量不报警(24个正常), 只报"长边方向与位置矛盾"(如右侧柱长边朝X)
                hits.append((pid, pit['名称'], '疑似方向反转 %d 个(需人工核对)' % report['orient_flips']))
            elif pid == 'PIT-06' and report.get('main_match_rate') is not None:
                # 校准: 只查主区贴交点率(夹层柱不贴主轴网是设计如此, 不计入)
                if report['main_match_rate'] < 0.9:
                    hits.append((pid, pit['名称'], '主区柱贴交点率 %.0f%%' % (100 * report['main_match_rate'])))
            elif pid == 'PIT-08' and report.get('coord_outliers'):
                # 校准: 实体数差异不报警(含SOLID/模板矩形), 只报坐标越界
                if report['coord_outliers'] > 0:
                    hits.append((pid, pit['名称'], '%d 个柱坐标超主轴网范围 ±1km' % report['coord_outliers']))
        return hits

    # ---------- 项目画像入库 ----------
    def save_profile(self, profile):
        """profile: dict(单项目画像)。按 id 去重写入 project_profiles.json"""
        self.profiles.setdefault('项目', [])
        for i, p in enumerate(self.profiles['项目']):
            if p.get('id') == profile.get('id'):
                self.profiles['项目'][i] = profile
                break
        else:
            self.profiles['项目'].append(profile)
        self._save(PROFILES, self.profiles)
