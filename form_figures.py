"""Animated wrong-vs-right form figures for the Streamlit mockup.

A small JavaScript port of the figure engine in the web app (web/src/components/FormCheck.tsx): side-view
stick figures built from joint angles, so every limb keeps its length. Returns one self-contained HTML page.
"""
import json

ANIMS = {
    "hinge": {"ideal": True,
        "right": [{"shin": 0, "thigh": 0, "torso": 0, "uarm": 180, "farm": 180}, {"shin": 8, "thigh": -48, "torso": 58, "uarm": 180, "farm": 178}],
        "wrong": [{"shin": 0, "thigh": 0, "torso": 0, "uarm": 180, "farm": 180}, {"shin": 24, "thigh": -30, "torso": 42, "uarm": 196, "farm": 200, "bend": 20, "head": -28}]},
    "swing": {
        "right": [{"shin": 6, "thigh": -50, "torso": 60, "uarm": 200, "farm": 210}, {"shin": 0, "thigh": 4, "torso": 2, "uarm": 82, "farm": 84}],
        "wrong": [{"shin": 26, "thigh": -70, "torso": 38, "uarm": 200, "farm": 215, "bend": 10}, {"shin": 0, "thigh": 10, "torso": -16, "uarm": 28, "farm": 14, "bend": -16}]},
    "squat": {"props": "box", "ideal": True,
        "right": [{"shin": 0, "thigh": 0, "torso": 0, "uarm": 172, "farm": 20}, {"shin": 30, "thigh": -85, "torso": 28, "uarm": 172, "farm": 20}],
        "wrong": [{"shin": 0, "thigh": 0, "torso": 0, "uarm": 172, "farm": 20}, {"shin": 40, "thigh": -80, "torso": 70, "uarm": 178, "farm": 30, "bend": 12, "head": -15}]},
    "row": {"ideal": True,
        "right": [{"shin": 6, "thigh": -40, "torso": 68, "uarm": 180, "farm": 180}, {"shin": 6, "thigh": -40, "torso": 68, "uarm": -72, "farm": 165}],
        "wrong": [{"shin": 6, "thigh": -40, "torso": 68, "uarm": 180, "farm": 180}, {"shin": 10, "thigh": -34, "torso": 46, "uarm": -55, "farm": 150, "bend": 14, "head": -12}]},
    "carry": {
        "right": [{"shin": -8, "thigh": 8, "torso": 0, "uarm": 180, "farm": 180}, {"shin": 8, "thigh": -8, "torso": 0, "uarm": 180, "farm": 180}],
        "wrong": [{"shin": -8, "thigh": 8, "torso": 18, "uarm": 190, "farm": 190, "bend": 10, "head": -14}, {"shin": 8, "thigh": -8, "torso": 18, "uarm": 190, "farm": 190, "bend": 10, "head": -14}]},
    "stairs": {"props": "steps", "lift": 14,
        "right": [{"shin": 18, "thigh": -30, "torso": 6, "uarm": 200, "farm": 190}, {"shin": 0, "thigh": 0, "torso": 4, "uarm": 200, "farm": 190}],
        "wrong": [{"shin": 30, "thigh": -55, "torso": 38, "uarm": 200, "farm": 190, "bend": 8}, {"shin": 6, "thigh": -8, "torso": 32, "uarm": 200, "farm": 190, "bend": 8}]},
    "pushup": {"root": "hand",
        "right": [{"h2e": -4, "e2s": 4, "s2h": -111, "h2k": -111, "k2a": -111}, {"h2e": -55, "e2s": 61, "s2h": -101, "h2k": -101, "k2a": -101}],
        "wrong": [{"h2e": -4, "e2s": 4, "s2h": -118, "h2k": -98, "k2a": -118}, {"h2e": -55, "e2s": 61, "s2h": -113, "h2k": -86, "k2a": -108, "head": -14}]},
}

_JS = r"""
const SPEC=__SPEC__, BELL=__BELL__, FLOOR=140, L={shin:30,thigh:30,torso:38,uarm:18,farm:16,head:14};
const rad=d=>d*Math.PI/180, P=(o,d,l)=>[o[0]+l*Math.sin(rad(d)),o[1]-l*Math.cos(rad(d))], f=n=>n.toFixed(1);
const css=n=>getComputedStyle(document.body).getPropertyValue(n)||'';
function mix(a,b,u){const o={};new Set([...Object.keys(a),...Object.keys(b)]).forEach(k=>o[k]=(a[k]||0)+((b[k]||0)-(a[k]||0))*u);return o;}
function solve(p){
 if(SPEC.root==='hand'){const hand=[150,FLOOR-2],el=P(hand,p.h2e||0,L.farm),sh=P(el,p.e2s||0,L.uarm),hip=P(sh,p.s2h||-105,L.torso),kn=P(hip,p.h2k||-105,L.thigh),an=P(kn,p.k2a||-105,L.shin),hd=P(sh,75+(p.head||0),L.head);
  return {an,kn,hip,sh,el,hand,hd,ct:[(sh[0]+hip[0])/2,(sh[1]+hip[1])/2]};}
 const an=[100,FLOOR-3-(SPEC.lift||0)],kn=P(an,p.shin||0,L.shin),hip=P(kn,p.thigh||0,L.thigh),t=p.torso||0,sh=P(hip,t,L.torso),
  el=P(sh,p.uarm===undefined?180:p.uarm,L.uarm),hand=P(el,p.farm===undefined?180:p.farm,L.farm),mid=[(hip[0]+sh[0])/2,(hip[1]+sh[1])/2],
  dx=Math.sin(rad(t)),dy=-Math.cos(rad(t)),b=(p.bend||0)*2,ct=[mid[0]+dy*b,mid[1]-dx*b],
  tang=Math.atan2(sh[0]-ct[0],-(sh[1]-ct[1]))*180/Math.PI,hd=P(sh,tang+(p.head||0),L.head);
 return {an,kn,hip,sh,el,hand,hd,ct};}
function props(bad){
 if(SPEC.props==='box')return '<rect x="52" y="111" width="46" height="29" rx="3" class="blk"/>';
 if(SPEC.props==='steps'){const x0=bad?106:80;return `<rect x="${x0}" y="126" width="${166-x0}" height="14" class="blk"/><rect x="150" y="112" width="60" height="28" class="blk"/>`;}
 return '';}
function fig(p,bad){
 const j=solve(p),toe=[j.an[0]+13,j.an[1]+3],sp=bad?'#dc2626':'#16a34a',
  line=(pts,c,w)=>`<path d="M${pts.map(q=>f(q[0])+' '+f(q[1])).join(' L')}" fill="none" stroke="${c}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
 let s=`<line x1="0" y1="${FLOOR}" x2="200" y2="${FLOOR}" stroke="#bbb" stroke-width="2"/>`+props(bad);
 if(BELL)s+=`<circle cx="${f(j.hand[0])}" cy="${f(j.hand[1]+8)}" r="7" fill="${C}"/><path d="M${f(j.hand[0]-5)} ${f(j.hand[1]+3)} Q${f(j.hand[0])} ${f(j.hand[1]-5)} ${f(j.hand[0]+5)} ${f(j.hand[1]+3)}" fill="none" stroke="${C}" stroke-width="3.5" stroke-linecap="round"/>`;
 if(!bad&&SPEC.ideal){const d=[j.sh[0]-j.hip[0],j.sh[1]-j.hip[1]];s+=`<line x1="${f(j.hip[0]-d[0]*.35)}" y1="${f(j.hip[1]-d[1]*.35)}" x2="${f(j.sh[0]+d[0]*.5)}" y2="${f(j.sh[1]+d[1]*.5)}" stroke="#16a34a" stroke-width="1.5" stroke-dasharray="3 3" opacity=".7"/>`;}
 s+=line([j.an,toe],C,5)+line([j.an,j.kn,j.hip],C,5);
 s+=`<path d="M${f(j.hip[0])} ${f(j.hip[1])} Q${f(j.ct[0])} ${f(j.ct[1])} ${f(j.sh[0])} ${f(j.sh[1])}" fill="none" stroke="${sp}" stroke-width="6" stroke-linecap="round"/>`;
 s+=line([j.sh,j.el,j.hand],C,5)+`<circle cx="${f(j.hd[0])}" cy="${f(j.hd[1])}" r="7" fill="#fff" stroke="${C}" stroke-width="4"/>`;
 return s;}
const C=getComputedStyle(document.documentElement).color||'#222', A=document.getElementById('a'), B=document.getElementById('b');
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
function draw(u){A.innerHTML=fig(mix(SPEC.wrong[0],SPEC.wrong[1],u),true);B.innerHTML=fig(mix(SPEC.right[0],SPEC.right[1],u),false);}
if(reduce)draw(1);else{const t0=performance.now();(function tick(t){const s=((t-t0)/3200)%1,c=s<.1?0:s<.45?(s-.1)/.35:s<.6?1:s<.95?1-(s-.6)/.35:0;draw(.5-.5*Math.cos(Math.PI*c));requestAnimationFrame(tick);})(t0);}
"""


def figures_html(anim: str, bell: bool = False) -> str | None:
    spec = ANIMS.get(anim or "")
    if not spec:
        return None
    js = _JS.replace("__SPEC__", json.dumps(spec)).replace("__BELL__", "true" if bell else "false")
    return f"""<style>
body{{margin:0;font-family:system-ui,sans-serif;color:#1d2024;background:transparent}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.box{{position:relative;border-radius:12px;background:#eceeef;overflow:hidden}}
.box svg{{display:block;width:100%}}
.tag{{position:absolute;top:6px;left:6px;color:#fff;font-size:12px;font-weight:700;border-radius:99px;padding:2px 9px}}
.blk{{fill:#fff;stroke:#8a8f95;stroke-width:1.2}}
</style>
<div class="row">
<div class="box" style="box-shadow:inset 0 0 0 1.5px #dc2626"><span class="tag" style="background:#dc2626">✕ Wrong</span><svg id="a" viewBox="0 0 200 160"></svg></div>
<div class="box" style="box-shadow:inset 0 0 0 1.5px #16a34a"><span class="tag" style="background:#16a34a">✓ Right</span><svg id="b" viewBox="0 0 200 160"></svg></div>
</div>
<script>{js}</script>"""
