"""Convert the manuscript's Elsevier-Harvard reference list into
(a) Taylor & Francis Chicago author-date (CAD) and (b) APA 7 entries.
Usage: python refs_convert.py manuscript.md [doi_map.json] > out.md
Prints two blocks: ## CAD and ## APA, one entry per line, in the original order.
Entries that do not parse as journal articles are handled via MANUAL below.
"""
import sys, re, json

SMALL = {'a','an','the','and','but','or','for','nor','of','in','on','at','to','by','vs.','vs','with','from','as','into','over','via','per','versus'}
def _cap(core):
    parts = core.split('-')
    return '-'.join((p[:1].upper() + p[1:]) if p else p for p in parts)

def headline(title):
    """Chicago headline-style capitalisation of a sentence-case title."""
    words = title.split(' ')
    out = []
    after_break = True
    n = len(words)
    for i, w in enumerate(words):
        segs = w.split('—')
        new_segs = []
        for si, seg in enumerate(segs):
            core = re.sub(r'^[\(\"“\']+|[\)\"”,:;?!\.]+$', '', seg)
            lw = core.lower()
            first = (i == 0 and si == 0) or after_break or si > 0
            last = (i == n - 1)
            before_colon = seg.endswith(':')
            if core and any(pp[:1].islower() for pp in core.split('-')) and (first or last or before_colon or lw not in SMALL):
                seg = seg.replace(core, _cap(core), 1)
            new_segs.append(seg)
        w = '—'.join(new_segs)
        after_break = w.endswith(':') or w.endswith('?') or w.endswith('!')
        out.append(w)
    return ' '.join(out)

def parse_authors(s):
    # "Adner, R., Kapoor, R." -> [("Adner","R."),("Kapoor","R.")]; handles "von Krogh, G." and "de Grazia"
    parts = [p.strip() for p in re.split(r',\s*', s)]
    auth = []
    i = 0
    while i < len(parts) - 1:
        auth.append((parts[i], parts[i+1]))
        i += 2
    return auth

def space_initials(ini):
    ini = ini.replace('.-', '.-')
    return re.sub(r'\.(?=[A-Z])', '. ', ini)

def cad_authors(auth):
    if len(auth) == 1:
        return f"{auth[0][0]}, {space_initials(auth[0][1]).rstrip('.')}"
    first = f"{auth[0][0]}, {space_initials(auth[0][1])}"
    rest = [f"{space_initials(i)} {s}" for s, i in auth[1:]]
    if len(auth) == 2:
        return f"{first}, and {rest[0]}"
    return f"{first}, " + ", ".join(rest[:-1]) + f", and {rest[-1]}"

def apa_authors(auth):
    names = [f"{s}, {space_initials(i)}" for s, i in auth]
    if len(names) == 1: return names[0]
    if len(names) == 2: return f"{names[0]}, & {names[1]}"
    return ", ".join(names[:-1]) + f", & {names[-1]}"

ART = re.compile(r'^(?P<authors>.+?), (?P<year>\d{4}[a-z]?)\. (?P<title>.+?)(?P<tend>[.?!]) (?P<journal>[^.?]+?) (?P<vol>[\d–-]+)(?: \((?P<issue>[^)]+)\))?(?:, (?P<pages>[\w–-]+))?\.(?: (?P<doi>https://doi\.org/\S+))?$')

# manual entries keyed by the start of the original line
MANUAL = {
 'Arden, W.': ('Arden, W., M. Brillouët, P. Cogez, M. Graef, B. Huizing, and R. Mahnkopf. 2010. *"More-than-Moore" White Paper*. International Technology Roadmap for Semiconductors (ITRS).',
               'Arden, W., Brillouët, M., Cogez, P., Graef, M., Huizing, B., & Mahnkopf, R. (2010). *"More-than-Moore" white paper*. International Technology Roadmap for Semiconductors (ITRS).'),
 'Brown, C., Linden': ('Brown, C., and G. Linden. 2009. *Chips and Change: How Crisis Reshapes the Semiconductor Industry*. Cambridge, MA: MIT Press.',
               'Brown, C., & Linden, G. (2009). *Chips and change: How crisis reshapes the semiconductor industry*. MIT Press.'),
 'Cameron, A.C.': ('Cameron, A. C., and P. K. Trivedi. 2013. *Regression Analysis of Count Data*. 2nd ed. Cambridge: Cambridge University Press.',
               'Cameron, A. C., & Trivedi, P. K. (2013). *Regression analysis of count data* (2nd ed.). Cambridge University Press.'),
 'European Patent Office': ('European Patent Office. 2025. *PATSTAT Global* (2025 edition) [Database]. Vienna: EPO.',
               'European Patent Office. (2025). *PATSTAT Global* (2025 ed.) [Database]. EPO.'),
 'Hall, B.H., Trajtenberg': ('Hall, B. H., and M. Trajtenberg. 2004. *Uncovering GPTs with Patent Data*. NBER Working Paper 10901. Cambridge, MA: National Bureau of Economic Research. https://doi.org/10.3386/w10901.',
               'Hall, B. H., & Trajtenberg, M. (2004). *Uncovering GPTs with patent data* (NBER Working Paper No. 10901). National Bureau of Economic Research. https://doi.org/10.3386/w10901'),
 'Hosmer, D.W.': ('Hosmer, D. W., S. Lemeshow, and R. X. Sturdivant. 2013. *Applied Logistic Regression*. 3rd ed. Hoboken, NJ: Wiley.',
               'Hosmer, D. W., Lemeshow, S., & Sturdivant, R. X. (2013). *Applied logistic regression* (3rd ed.). Wiley.'),
 'Langlois, R.N.': ('Langlois, R. N., and W. E. Steinmueller. 1999. "The Evolution of Competitive Advantage in the Worldwide Semiconductor Industry, 1947–1996." In *Sources of Industrial Leadership: Studies of Seven Industries*, edited by D. C. Mowery and R. R. Nelson, 19–78. Cambridge: Cambridge University Press.',
               'Langlois, R. N., & Steinmueller, W. E. (1999). The evolution of competitive advantage in the worldwide semiconductor industry, 1947–1996. In D. C. Mowery & R. R. Nelson (Eds.), *Sources of industrial leadership: Studies of seven industries* (pp. 19–78). Cambridge University Press.'),
 'Macher, J.T.': ('Macher, J. T., and D. C. Mowery. 2004. "Vertical Specialization and Industry Structure in High Technology Industries." In *Business Strategy over the Industry Lifecycle*, Advances in Strategic Management, vol. 21, edited by J. A. C. Baum and A. M. McGahan, 317–356. Bingley: Emerald.',
               'Macher, J. T., & Mowery, D. C. (2004). Vertical specialization and industry structure in high technology industries. In J. A. C. Baum & A. M. McGahan (Eds.), *Business strategy over the industry lifecycle* (Advances in Strategic Management, Vol. 21, pp. 317–356). Emerald.'),
 'McFadden, D.': ('McFadden, D. 1974. "Conditional Logit Analysis of Qualitative Choice Behavior." In *Frontiers in Econometrics*, edited by P. Zarembka, 105–142. New York: Academic Press.',
               'McFadden, D. (1974). Conditional logit analysis of qualitative choice behavior. In P. Zarembka (Ed.), *Frontiers in econometrics* (pp. 105–142). Academic Press.'),
 'Yole Group': ('Yole Group. 2024. *Hybrid Bonding Patent Landscape Analysis 2024*. Industry report. Sophia Antipolis: KnowMade (Yole Group).',
               'Yole Group. (2024). *Hybrid bonding patent landscape analysis 2024* [Industry report]. KnowMade (Yole Group).'),
}

def convert(md, doi_map=None):
    refs = md.split('## 참고문헌')[1] if '## 참고문헌' in md else md.split('## References')[1]
    cad, apa, unparsed = [], [], []
    for ln in refs.split('\n'):
        ln = ln.strip()
        if not ln or ln.startswith('#') or ln.startswith('('): continue
        m = None
        key = next((k for k in MANUAL if ln.startswith(k)), None)
        if key:
            cad.append(MANUAL[key][0]); apa.append(MANUAL[key][1]); continue
        m = ART.match(ln)
        if not m:
            unparsed.append(ln); cad.append('UNPARSED: ' + ln); apa.append('UNPARSED: ' + ln); continue
        d = m.groupdict()
        if d['tend'] in '?!':
            d['title'] = d['title'] + d['tend']
        auth = parse_authors(d['authors'])
        doi = d.get('doi') or (doi_map or {}).get(ln[:40]) or ''
        doi = doi.rstrip('.')
        if doi and not doi.startswith('http'):
            doi = 'https://doi.org/' + doi
        title_c = headline(d['title'])
        # Chicago: single quotes inside a quoted title; no period after a title ending in ? or !
        inner = re.sub(r'"([^"]*)"', r"'\1'", title_c)
        title_c = inner
        tpunct = '' if title_c.endswith(('?', '!')) else '.'
        apunct = '' if d['title'].endswith(('?', '!')) else '.'
        vol_c = f"{d['vol']}" + (f" ({d['issue']})" if d['issue'] else '')
        pages = d['pages'] or ''
        cad_e = f"{cad_authors(auth).rstrip('.')}. {d['year']}. \"{title_c}{tpunct}\" *{d['journal']}* {vol_c}" + (f": {pages}" if pages else '') + "." + (f" {doi}." if doi else '')
        apa_e = f"{apa_authors(auth)} ({d['year']}). {d['title']}{apunct} *{d['journal']}*, *{d['vol']}*" + (f"({d['issue']})" if d['issue'] else '') + (f", {pages}" if pages else '') + "." + (f" {doi}" if doi else '')
        cad.append(cad_e); apa.append(apa_e)
    return cad, apa, unparsed

if __name__ == '__main__':
    md = open(sys.argv[1], encoding='utf-8').read()
    doi_map = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None
    cad, apa, un = convert(md, doi_map)
    print('## CAD'); print('\n\n'.join(cad)); print('\n## APA'); print('\n\n'.join(apa))
    print('\n## UNPARSED', len(un), file=sys.stderr)
