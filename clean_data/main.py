import sys
import re
import bz2
import os.path
import json
from html.entities import name2codepoint
import opencc
from pybloom_live import ScalableBloomFilter

prefix = None
keepLinks = False
keepSections = False
# w: Internal links to the Wikipedia
acceptedNamespaces = set(['w'])
discardElements = set([
        'gallery', 'timeline', 'noinclude', 'pre',
        'table', 'tr', 'td', 'th', 'caption',
        'form', 'input', 'select', 'option', 'textarea',
        'ul', 'li', 'ol', 'dl', 'dt', 'dd', 'menu', 'dir',
        'ref', 'references', 'img', 'imagemap', 'source'
        ])

bloom = ScalableBloomFilter(10000, 0.001)

def process(id, title, raw_text):
    title = cc.convert(title)
    meta = { "id": id, "title": title }
    text = clean(raw_text, False)
    text = compact(text, structure=True)
    text = "".join(text)
    return { "text": text, "meta": meta }

selfClosingTags = [ 'br', 'hr', 'nobr', 'ref', 'references' ]
ignoredTags = [
        'b', 'big', 'blockquote', 'center', 'cite', 'div', 'em',
        'font', 'h1', 'h2', 'h3', 'h4', 'hiero', 'i', 'kbd', 'nowiki',
        'p', 'plaintext', 's', 'small', 'span', 'strike', 'strong',
        'sub', 'sup', 'tt', 'u', 'var',
]
placeholder_tags = {'math':'formula', 'code':'codice'}


def unescape(text):
    def fixup(m):
        text = m.group(0)
        code = m.group(1)
        try:
            if text[1] == "#":  # character reference
                if text[2] == "x":
                    return chr(int(code[1:], 16))
                else:
                    return chr(int(code))
            else:               # named entity
                return chr(name2codepoint[code])
        except:
            return text # leave as is

    return re.sub(r"&#?(\w+);", fixup, text)

# Match HTML comments
comment = re.compile(r'<!--.*?-->', re.DOTALL)

# Match elements to ignore
discard_element_patterns = []
for tag in discardElements:
    pattern = re.compile(r'<\s*%s\b[^>]*>.*?<\s*/\s*%s>' % (tag, tag), re.DOTALL | re.IGNORECASE)
    discard_element_patterns.append(pattern)

# Match ignored tags
ignored_tag_patterns = []
def ignoreTag(tag):
    left = re.compile(r'<\s*%s\b[^>]*>' % tag, re.IGNORECASE)
    right = re.compile(r'<\s*/\s*%s>' % tag, re.IGNORECASE)
    ignored_tag_patterns.append((left, right))

for tag in ignoredTags:
    ignoreTag(tag)

# Match selfClosing HTML tags
selfClosing_tag_patterns = []
for tag in selfClosingTags:
    pattern = re.compile(r'<\s*%s\b[^/]*/\s*>' % tag, re.DOTALL | re.IGNORECASE)
    selfClosing_tag_patterns.append(pattern)

# Match HTML placeholder tags
placeholder_tag_patterns = []
for tag, repl in list(placeholder_tags.items()):
    pattern = re.compile(r'<\s*%s(\s*| [^>]+?)>.*?<\s*/\s*%s\s*>' % (tag, tag), re.DOTALL | re.IGNORECASE)
    placeholder_tag_patterns.append((pattern, repl))

# Match preformatted lines
preformatted = re.compile(r'^ .*?$', re.MULTILINE)

# Match external links (space separates second optional parameter)
externalLink = re.compile(r'\[\w+.*? (.*?)\]')
externalLinkNoAnchor = re.compile(r'\[\w+[&\]]*\]')

# Matches bold/italic
bold_italic = re.compile(r"'''''([^']*?)'''''")
bold = re.compile(r"'''(.*?)'''")
italic_quote = re.compile(r"''\"(.*?)\"''")
italic = re.compile(r"''([^']*)''")
quote_quote = re.compile(r'""(.*?)""')

# Matches space
spaces = re.compile(r' {2,}')

# Matches dots
dots = re.compile(r'\.{4,}')

empty_pa = re.compile(r'（\W*）')
multi_lang_1 = re.compile(r'-\{([^;\}]*)\}-')
multi_lang_2 = re.compile(r'-\{[a-z\-]+:([^;\}]+);[^\}]*\}-')

# A matching function for nested expressions, e.g. namespaces and tables.
def dropNested(text, openDelim, closeDelim):
    openRE = re.compile(openDelim)
    closeRE = re.compile(closeDelim)
    # partition text in separate blocks { } { }
    matches = []                # pairs (s, e) for each partition
    nest = 0                    # nesting level
    start = openRE.search(text, 0)
    if not start:
        return text
    end = closeRE.search(text, start.end())
    next = start
    while end:
        next = openRE.search(text, next.end())
        if not next:            # termination
            while nest:         # close all pending
                nest -=1
                end0 = closeRE.search(text, end.end())
                if end0:
                    end = end0
                else:
                    break
            matches.append((start.start(), end.end()))
            break
        while end.end() < next.start():
            # { } {
            if nest:
                nest -= 1
                # try closing more
                last = end.end()
                end = closeRE.search(text, end.end())
                if not end:     # unbalanced
                    if matches:
                        span = (matches[0][0], last)
                    else:
                        span = (start.start(), last)
                    matches = [span]
                    break
            else:
                matches.append((start.start(), end.end()))
                # advance start, find next close
                start = next
                end = closeRE.search(text, next.end())
                break           # { }
        if next != start:
            # { { }
            nest += 1
    # collect text outside partitions
    res = ''
    start = 0
    for s, e in  matches:
        res += text[start:s]
        start = e
    res += text[start:]
    return res

def dropSpans(matches, text):
    """Drop from text the blocks identified in matches"""
    matches.sort()
    res = ''
    start = 0
    for s, e in matches:
        res += text[start:s]
        start = e
    res += text[start:]
    return res

# Match interwiki links, | separates parameters.
# First parameter is displayed, also trailing concatenated text included
# in display, e.g. s for plural).
#
# Can be nested [[File:..|..[[..]]..|..]], [[Category:...]], etc.
# We first expand inner ones, than remove enclosing ones.
#
wikiLink = re.compile(r'\[\[([^[]*?)(?:\|([^[]*?))?\]\](\w*)')

parametrizedLink = re.compile(r'\[\[.*?\]\]')

# Function applied to wikiLinks
def make_anchor_tag(match):
    global keepLinks
    link = match.group(1)
    colon = link.find(':')
    if colon > 0 and link[:colon] not in acceptedNamespaces:
        return ''
    trail = match.group(3)
    anchor = match.group(2)
    if not anchor:
        anchor = link
    anchor += trail
    if keepLinks:
        return '<a href="%s">%s</a>' % (link, anchor)
    else:
        return anchor

cc = opencc.OpenCC('t2s')

def clean(text, structure):

    # FIXME: templates should be expanded
    # Drop transclusions (template, parser functions)
    # See: http://www.mediawiki.org/wiki/Help:Templates
    text = dropNested(text, r'{{', r'}}')

    # Drop tables
    text = dropNested(text, r'{\|', r'\|}')

    # Expand links
    text = wikiLink.sub(make_anchor_tag, text)
    # Drop all remaining ones
    text = parametrizedLink.sub('', text)

    # Handle external links
    text = externalLink.sub(r'\1', text)
    text = externalLinkNoAnchor.sub('', text)

    # Handle bold/italic/quote
    text = bold_italic.sub(r'\1', text)
    text = bold.sub(r'\1', text)
    text = italic.sub(r'\1', text)
    text = italic_quote.sub(r'&quot;\1&quot;', text)
    text = quote_quote.sub(r'&quot;\1&quot;', text)
    text = text.replace("'''", '').replace("''", '&quot;')

    ################ Process HTML ###############

    # turn into HTML
    text = unescape(text)
    # do it again (&amp;nbsp;)
    text = unescape(text)

    # Collect spans

    matches = []
    # Drop HTML comments
    for m in comment.finditer(text):
        matches.append((m.start(), m.end()))

    # Drop self-closing tags
    for pattern in selfClosing_tag_patterns:
        for m in pattern.finditer(text):
            matches.append((m.start(), m.end()))

    # Drop ignored tags
    for left, right in ignored_tag_patterns:
        for m in left.finditer(text):
            matches.append((m.start(), m.end()))
        for m in right.finditer(text):
            matches.append((m.start(), m.end()))

    # Bulk remove all spans
    text = dropSpans(matches, text)

    # Cannot use dropSpan on these since they may be nested
    # Drop discarded elements
    for pattern in discard_element_patterns:
        text = pattern.sub('', text)

    # Expand placeholders
    for pattern, placeholder in placeholder_tag_patterns:
        index = 1
        for match in pattern.finditer(text):
            text = text.replace(match.group(), '%s_%d' % (placeholder, index))
            index += 1

    #######################################

    # Drop preformatted
    # This can't be done before since it may remove tags
    text = preformatted.sub('', text)

    # Cleanup text
    text = text.replace('<<', '《').replace('>>', '》')
    text = text.replace('(', '（').replace(')', '）')
    text = text.replace(',,', ',').replace(',.', '.')
    text = text.replace('，，', '，')
    text = multi_lang_1.sub(r'\1', text)
    text = multi_lang_2.sub(r'\1', text)

    text = empty_pa.sub('', text)
    text = text.replace('（，', '（').replace('，）', '）')
    text = text.replace('\t', ' ')  # tab -> space
    text = spaces.sub(' ', text)  # multi space -> space
    text = dots.sub('...', text)  # multi dots -> three dots
    text = re.sub(r' (,:\.）】》)', r'\1', text)  # space before punctuations
    text = re.sub(r'(【（《) ', r'\1', text)  # space after left
    text = re.sub(r'\n\W+?\n', '\n', text) # lines with only punctuations
    
    re2 = re.compile(r"__[A-Z]+__")
    text = re2.sub("", text)
    #Add other filters here
    
    # traditional to simplified
    text = cc.convert(text)
    return text

section = re.compile(r'(==+)\s*(.*?)\s*\1')
lists = re.compile(r'^([#\*]+)\s*(.*)')
reference_titles = {
    "外部连接", "外部连结", "外部链接", "参考文献", "参见", "相关条目", "相关链接", "相关连接", "另见", "延伸阅读", "参阅", "参考资料", "内部链接"
}

def compact(text, structure=False):
    """Deal with headers, lists, empty sections, residuals of tables"""
    page = []                   # list of paragraph
    headers = {}                # Headers for unfilled sections
    emptySection = False        # empty sections are discarded
    inList = False              # whether opened <UL>

    for line in text.split('\n'):

        if not line:
            continue
        if line.startswith("参考资料："):
            continue
        if structure:
            if line[0] == ';': # dt (bold)
                line = line[1:].strip()
            elif line[0] == ':': # dd (indent)
                line = "  " + line[1:].strip()
            else: # ul & ol
                m = lists.match(line)
                if m:
                    # empty item
                    if not m.group(2):
                        continue
                    lvl = len(m.group(1))
                    line = "  "*(lvl-1) + "- " + m.group(2)
        # Handle section titles
        m = section.match(line)
        if m:
            title = m.group(2)
            # discard references and external links
            if title in reference_titles:
                break
            lvl = len(m.group(1))
            if structure:
                title = "#"*lvl + " " + title
            headers[lvl] = title
            # drop previous headers
            for i in list(headers.keys()):
                if i > lvl:
                    del headers[i]
            emptySection = True
            continue
        # Handle page title
        if line.startswith('++'):
            title = line[2:-2]
            if title:
                page.append(title)
        # Drop residuals of lists
        elif line[0] in '{|' or line[-1] in '}':
            continue
        # Drop irrelevant lines
        elif (line[0] == '(' and line[-1] == ')') or line.strip('.-') == '':
            continue
        elif len(headers):
            items = list(headers.items())
            items.sort()
            for (i, v) in items:
                page.append(v)
            headers.clear()
            page.append(line)   # first line
            emptySection = False
        elif not emptySection:
            page.append(line)

    return page

def handle_unicode(entity):
    numeric_code = int(entity[2:-1])
    if numeric_code >= 0x10000: return ''
    return chr(numeric_code)


re_chinese = re.compile(r'[\u4e00-\u9fff]')
re_punct = re.compile(r'\W')
re_long_non_chinese = re.compile(r'[^\u4e00-\u9fff]{10,}')

def filter_text(text):
    total_len = len(text)
    if total_len == 0:
        return False

    chinese_chars = re.findall(re_chinese, text)
    num_chinese = len(chinese_chars)
    punct_chars = re.findall(re_punct, text)
    num_punct = len(punct_chars)
    chinese_ratio = num_chinese / total_len
    punct_ratio = num_punct / total_len

    if chinese_ratio < 0.5:
        return False
    if num_chinese < 10:
        return False
    if punct_ratio > 0.2:
        return False
    if re_long_non_chinese.search(text):
        return False
    return True

tagRE = re.compile(r'(.*?)<(/?\w+)[^>]*>(?:([^<]*)(<.*?>)?)?')

def process_data(input, ):
    filtered_data = []
    page = []
    id = None
    inText = False
    redirect = False
    for line in input:
        line = str(line.decode('utf-8'))
        tag = ''
        if '<' in line:
            m = tagRE.search(line)
            if m:
                tag = m.group(2)
        if tag == 'page':
            page = []
            redirect = False
        elif tag == 'id' and not id:
            id = m.group(3)
        elif tag == 'title':
            title = m.group(3)
        elif tag == 'redirect':
            redirect = True
        elif tag == 'text':
            inText = True
            line = line[m.start(3):m.end(3)] + '\n'
            page.append(line)
            if m.lastindex == 4: # open-close same line
                inText = False
        elif tag == '/text':
            if m.group(1):
                page.append(m.group(1) + '\n')
            inText = False
        elif inText:
            page.append(line)
        elif tag == 'math':
            print(line)
        elif tag == '/page':
            idx = title.find(':')
            # Discard Redirect & Category & Template & Wikipedia & File etc.
            if redirect or (idx >= 0 and title[:idx] not in acceptedNamespaces):
                id, page = None, []
                continue
            print(id, title)
            sys.stdout.flush()
            item = process(id, title, ''.join(page))

            id = None
            page = []

            if not filter_text(item["text"]):
                continue
            if item["text"] in bloom:
                continue
            bloom.add(item["text"])

            filtered_data.append(item)

            if len(filtered_data) >= 1000:
                break
    return filtered_data

def main():
    global keepLinks, keepSections, prefix, acceptedNamespaces
    keepSections = True
    if not keepLinks:
        ignoreTag('a')

    fname = "zhwiki-20250201-pages-articles-multistream4.xml-p2889649p3391029.bz2"
    # fname = "zhwiki-20250201-pages-articles-multistream.xml.bz2"
    f = bz2.BZ2File(fname, mode='r')
    data = process_data(f)

    with open("data.jsonl", "a", encoding="utf-8") as fout:
        for d in data:
            json.dump(d, fout, ensure_ascii=False)
            fout.write("\n")

if __name__ == '__main__':
    main()
