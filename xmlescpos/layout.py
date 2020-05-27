# -*- coding: utf-8 -*-

from __future__ import absolute_import
import io
import base64
import math
import hashlib
import re
import xml.etree.ElementTree as ET
from PIL import Image

from escpos.constants import *

import logging
import six
_logger = logging.getLogger(__name__)


def utfstr(stuff):
    """ converts stuff to string and does without failing if stuff is a utf8 string """
    if isinstance(stuff, six.string_types):
        return stuff
    else:
        return str(stuff)


class StyleStack:
    """As we move through the the layout document, this keeps track of
    the changing styles. We then can push the current desired styles
    to the printer.

    The "width" has a special "auto" value, which will read the
    column width for the current font from the printer profile.
    """

    def __init__(self, profile):
        self.profile = profile
        self.stack = []
        self.defaults = {   # default style values
            'align': 'left',
            'underline': 'off',
            'bold': 'off',
            'size': 'normal',
            'font': 'a',
            'width': 'auto',
            'indent': 0,
            'tabwidth': 2,
            'bullet': ' - ',
            'line-ratio': 0.5,
            'color': 'black',

            'value-decimals': 2,
            'value-symbol': '',
            'value-symbol-position': 'after',
            'value-autoint': 'off',
            'value-decimals-separator': '.',
            'value-thousands-separator': ',',
            'value-width': 0,
        }

        self.types = {  # attribute types, default is string and can be ommitted
            'width': lambda v: v if v == 'auto' else int(v),
            'indent': 'int',
            'tabwidth': 'int',
            'line-ratio': 'float',
            'value-decimals': 'int',
            'value-width': 'int',
        }

        self.cmds = {
            # translation from styles to escpos commands
            # some style do not correspond to escpos command are used by
            # the serializer instead
            'align': {
                'left': TXT_STYLE['align']['left'],
                'right': TXT_STYLE['align']['right'],
                'center':  TXT_STYLE['align']['center'],
                '_order': 1,
            },
            'underline': {
                'off': TXT_STYLE['underline'].get(0),
                'on': TXT_STYLE['underline'].get(1),
                'double':  TXT_STYLE['underline'].get(2),
                # must be issued after 'size' command
                # because ESC ! resets ESC -
                '_order': 10,
            },
            'bold': {
                'off': TXT_STYLE['bold'].get(False),
                'on': TXT_STYLE['bold'].get(True),
                # must be issued after 'size' command
                # because ESC ! resets ESC -
                '_order': 10,
            },
            'font': {
                'a': TXT_FONT_A,
                'b': TXT_FONT_B,
                # must be issued after 'size' command
                # because ESC ! resets ESC -
                '_order': 10,
            },
            'size': {
                'normal': TXT_STYLE['size']['normal'],
                'double-height': TXT_STYLE['size']['2h'] ,
                'double-width': TXT_STYLE['size']['2w'],
                'double': TXT_STYLE['size']['2x'],
                '_order': 1,
            },
            'color': {
                'black':  TXT_STYLE['color']['black'],
                'red': TXT_STYLE['color']['red'],
                '_order': 1,
            }
        }

        self.push(self.defaults)

    def _get(self, style):
        """ what's the value of a style at the current stack level"""
        level = len(self.stack) - 1
        while level >= 0:
            if style in self.stack[level]:
                return self.stack[level][style]
            else:
                level = level - 1
        return None

    def get(self, style):
        value = self._get(style)

        if style == 'width' and value == 'auto':
            font = self._get('font')
            return self.profile.get_columns(font)

        return value


    def enforce_type(self, attr, val):
        """converts a value to the attribute's type"""
        if attr not in self.types:
            return utfstr(val)
        elif self.types[attr] == 'int':
            return int(float(val))
        elif self.types[attr] == 'float':
            return float(val)
        elif callable(self.types[attr]):
            return self.types[attr](val)
        else:
            return utfstr(val)

    def push(self, style={}):
        """push a new level on the stack with a style dictionnary containing style:value pairs"""
        _style = {}
        for attr in style:
            if attr in self.cmds and not style[attr] in self.cmds[attr]:
                _logger.warn('WARNING: ESC/POS PRINTING: ignoring invalid value: ' + utfstr(style[attr]) + ' for style: ' + utfstr(attr))
            else:
                _style[attr] = self.enforce_type(attr, style[attr])
        self.stack.append(_style)

    def set(self, style={}):
        """overrides style values at the current stack level"""
        _style = {}
        for attr in style:
            if attr in self.cmds and not style[attr] in self.cmds[attr]:
                _logger.warn('WARNING: ESC/POS PRINTING: ignoring invalid value: ' + utfstr(style[attr]) + ' for style: ' + utfstr(attr))
            else:
                self.stack[-1][attr] = self.enforce_type(attr, style[attr])

    def pop(self):
        """ pop a style stack level """
        if len(self.stack) > 1:
            self.stack = self.stack[:-1]

    def to_escpos(self):
        """ converts the current style to an escpos command string """
        cmd = b''
        ordered_cmds = list(self.cmds.keys())
        ordered_cmds.sort(
            key=lambda x: self.cmds[x]['_order'])
        for style in ordered_cmds:
            cmd += self.cmds[style][self.get(style)]
        return cmd


class XmlSerializer:
    """
    Converts the xml inline / block tree structure to a string,
    keeping track of newlines and spacings.
    The string is outputted asap to the provided escpos driver.
    """

    def __init__(self, printer):
        self.printer = printer
        self.stack = ['block']
        self.dirty = False

    def start_inline(self, stylestack=None):
        """ starts an inline entity with an optional style definition """
        self.stack.append('inline')
        if self.dirty:
            self.printer._raw(b' ')
        if stylestack:
            self.style(stylestack)

    def start_block(self, stylestack=None):
        """ starts a block entity with an optional style definition """
        if self.dirty:
            self.printer._raw(b'\n')
            self.dirty = False
        self.stack.append('block')
        if stylestack:
            self.style(stylestack)

    def end_entity(self):
        """ ends the entity definition. (but does not cancel the active style!) """
        if self.stack[-1] == 'block' and self.dirty:
            self.printer._raw(b'\n')
            self.dirty = False
        if len(self.stack) > 1:
            self.stack = self.stack[:-1]

    def pre(self, text):
        """ puts a string of text in the entity keeping the whitespace intact """
        if text:
            self.printer.text(text)
            self.dirty = True

    def text(self, text):
        """ puts text in the entity. Whitespace and newlines are stripped to single spaces. """
        if text:
            text = utfstr(text)
            text = text.strip()
            text = re.sub('\s+', ' ', text)
            if text:
                self.dirty = True
                self.printer.text(text)

    def linebreak(self):
        """ inserts a linebreak in the entity """
        self.dirty = False
        self.printer._raw(b'\n')

    def style(self, stylestack):
        """ apply a style to the entity (only applies to content added after the definition) """
        self.printer._raw(stylestack.to_escpos())

    def raw(self, raw):
        self.printer._raw(raw)


class XmlLineSerializer:
    """
    This is used to convert a xml tree into a single line, with a left and a right part.
    The content is not output to escpos directly, and is intended to be fedback to the
    XmlSerializer as the content of a block entity.
    """

    def __init__(self, indent=0, tabwidth=2, width=48, ratio=0.5):
        self.tabwidth = tabwidth
        self.indent = indent
        self.width = max(0, width - int(tabwidth * indent))
        self.lwidth = int(self.width * ratio)
        self.rwidth = max(0, self.width - self.lwidth)
        self.clwidth = 0
        self.crwidth = 0
        self.lbuffer = ''
        self.rbuffer = ''
        self.left = True

    def _txt(self, txt):
        if self.left:
            if self.clwidth < self.lwidth:
                txt = txt[:max(0, self.lwidth - self.clwidth)]
                self.lbuffer += txt
                self.clwidth += len(txt)
        else:
            if self.crwidth < self.rwidth:
                txt = txt[:max(0, self.rwidth - self.crwidth)]
                self.rbuffer += txt
                self.crwidth += len(txt)

    def start_inline(self, stylestack=None):
        if (self.left and self.clwidth) or (not self.left and self.crwidth):
            self._txt(' ')

    def start_block(self, stylestack=None):
        self.start_inline(stylestack)

    def end_entity(self):
        pass

    def pre(self, text):
        if text:
            self._txt(text)

    def text(self, text):
        if text:
            text = utfstr(text)
            text = text.strip()
            text = re.sub('\s+', ' ', text)
            if text:
                self._txt(text)

    def linebreak(self):
        pass

    def style(self, stylestack):
        pass

    def raw(self, raw):
        pass

    def start_right(self):
        self.left = False

    def get_line(self):
        return ' ' * self.indent * self.tabwidth + self.lbuffer + ' ' * \
            (self.width - self.clwidth - self.crwidth) + self.rbuffer


class Layout(object):
    """Main class. Parses an XML layout.

    Convert to ESC/POS. Send to a pyton-escpos printer object.

    Usage::

        from escpos import printer
        epson = printer.Dummy()
        Layout(xml).format(epson)
    """

    img_cache = {}

    def __init__(self, xml):
        self._root = root = ET.fromstring(xml.encode('utf-8'))

        self.slip_sheet_mode = False
        if 'sheet' in root.attrib and root.attrib['sheet'] == 'slip':
            self.slip_sheet_mode = True

        self.open_crashdrawer = 'open-cashdrawer' in root.attrib and \
            root.attrib['open-cashdrawer'] == 'true'

    def get_base64_image(self, img):
        id = hashlib.md5(img).hexdigest()

        if id not in self.img_cache:
            img = img[img.find(',') + 1:]
            f = io.BytesIO('img')
            f.write(base64.decodestring(img))
            f.seek(0)
            img_rgba = Image.open(f)
            #img = Image.new('RGB', img_rgba.size, (255, 255, 255))
            #channels = img_rgba.split()

            #if len(channels) > 1:
                ## use alpha channel as mask
                #img.paste(img_rgba, mask=channels[3])
            #else:
                #img.paste(img_rgba)

            self.img_cache[id] = img_rgba

        return self.img_cache[id]

    def print_elem(self, stylestack, serializer, elem, printer, indent=0):
        """Recursively print an element in the document.
        """

        elem_styles = {
            'h1': {'bold': 'on', 'size': 'double'},
            'h2': {'size': 'double'},
            'h3': {'bold': 'on', 'size': 'double-height'},
            'h4': {'size': 'double-height'},
            'h5': {'bold': 'on'},
            'em': {'font': 'b'},
            'b': {'bold': 'on'},
        }

        stylestack.push()
        if elem.tag in elem_styles:
            stylestack.set(elem_styles[elem.tag])
        stylestack.set(elem.attrib)

        if elem.tag in (
            'p',
            'div',
            'section',
            'article',
            'receipt',
            'header',
            'footer',
            'li',
            'h1',
            'h2',
            'h3',
            'h4',
                'h5'):
            serializer.start_block(stylestack)
            serializer.text(elem.text)
            for child in elem:
                self.print_elem(stylestack, serializer, child, printer)
                serializer.start_inline(stylestack)
                serializer.text(child.tail)
                serializer.end_entity()
            serializer.end_entity()

        elif elem.tag in ('span', 'em', 'b', 'left', 'right'):
            serializer.start_inline(stylestack)
            serializer.text(elem.text)
            for child in elem:
                self.print_elem(stylestack, serializer, child, printer)
                serializer.start_inline(stylestack)
                serializer.text(child.tail)
                serializer.end_entity()
            serializer.end_entity()

        elif elem.tag == 'value':
            serializer.start_inline(stylestack)
            serializer.pre(
                format_value(
                    elem.text,
                    decimals=stylestack.get('value-decimals'),
                    width=stylestack.get('value-width'),
                    decimals_separator=stylestack.get('value-decimals-separator'),
                    thousands_separator=stylestack.get('value-thousands-separator'),
                    autoint=(
                        stylestack.get('value-autoint') == 'on'),
                    symbol=stylestack.get('value-symbol'),
                    position=stylestack.get('value-symbol-position')))
            serializer.end_entity()

        elif elem.tag == 'line':
            width = stylestack.get('width')
            if stylestack.get('size') in ('double', 'double-width'):
                width = width / 2

            lineserializer = XmlLineSerializer(
                stylestack.get('indent') + indent,
                stylestack.get('tabwidth'),
                width,
                stylestack.get('line-ratio'))
            serializer.start_block(stylestack)
            for child in elem:
                if child.tag == 'left':
                    self.print_elem(
                        stylestack,
                        lineserializer,
                        child,
                        printer,
                        indent=indent)
                elif child.tag == 'right':
                    lineserializer.start_right()
                    self.print_elem(
                        stylestack,
                        lineserializer,
                        child,
                        printer,
                        indent=indent)
            serializer.pre(lineserializer.get_line())
            serializer.end_entity()

        elif elem.tag == 'ul':
            serializer.start_block(stylestack)
            bullet = stylestack.get('bullet')
            for child in elem:
                if child.tag == 'li':
                    serializer.style(stylestack)
                    serializer.raw(
                        ' ' * indent * stylestack.get('tabwidth') + bullet)
                self.print_elem(
                    stylestack,
                    serializer,
                    child,
                    printer,
                    indent=indent + 1)
            serializer.end_entity()

        elif elem.tag == 'ol':
            cwidth = len(str(len(elem))) + 2
            i = 1
            serializer.start_block(stylestack)
            for child in elem:
                if child.tag == 'li':
                    serializer.style(stylestack)
                    serializer.raw(' ' *
                                   indent *
                                   stylestack.get('tabwidth') +
                                   ' ' +
                                   (str(i) +
                                    ')').ljust(cwidth))
                    i = i + 1
                self.print_elem(
                    stylestack,
                    serializer,
                    child,
                    printer,
                    indent=indent + 1)
            serializer.end_entity()

        elif elem.tag == 'pre':
            serializer.start_block(stylestack)
            serializer.pre(elem.text)
            serializer.end_entity()

        elif elem.tag == 'hr':
            width = stylestack.get('width')
            if stylestack.get('size') in ('double', 'double-width'):
                width = width / 2
            serializer.start_block(stylestack)
            serializer.text(u'─' * width)
            serializer.end_entity()

        elif elem.tag == 'br':
            serializer.linebreak()

        elif elem.tag == 'img':
            if 'src' in elem.attrib and 'data:' in elem.attrib['src']:
                printer.image(self.get_base64_image(elem.attrib['src']))

        elif elem.tag == 'barcode' and 'encoding' in elem.attrib:
            serializer.start_block(stylestack)
            printer.barcode(strclean(elem.text), elem.attrib['encoding'])
            serializer.end_entity()
        
        elif elem.tag == 'qr':
            ec = int(elem.attrib.get('ec', QR_ECLEVEL_L))
            size = int(elem.attrib.get('size', 3))
            model = int(elem.attrib.get('model', QR_MODEL_2))
            center = bool(elem.attrib.get('center', False))
            native = bool(elem.attrib.get('native', False))
            impl = elem.attrib.get('impl', 'bitImageRaster')
            serializer.start_block(stylestack)
            printer.qr(elem.text, ec, size, model, native, center, impl)
            serializer.end_entity()
        
        elif elem.tag == 'cut':
            printer.cut()

        elif elem.tag == 'partialcut':
            printer.cut(mode='part')

        elif elem.tag == 'cashdraw':
            printer.cashdraw(2)
            printer.cashdraw(5)

        stylestack.pop()

    def format(self, printer):
        """Format the layout to print on the given printer driver.
        """

        stylestack = StyleStack(printer.profile)
        serializer = XmlSerializer(printer)
        root = self._root

        # Init the mode
        if self.slip_sheet_mode:
            printer._raw(SHEET_SLIP_MODE)
        else:
            printer._raw(SHEET_ROLL_MODE)

        # init tye styles
        printer._raw(stylestack.to_escpos())

        # Print the root element
        self.print_elem(stylestack, serializer, self._root, printer)

        # Finalize print actions: cut paper, open cashdrawer
        if self.open_crashdrawer:
            printer.cashdraw(2)
            printer.cashdraw(5)

        if 'cut' in root.attrib and root.attrib['cut'] == 'true':
            if self.slip_sheet_mode:
                printer._raw(CTL_FF)
            else:
                printer.cut()


def strclean(string):
    if not string:
        string = ''
    string = string.strip()
    string = re.sub('\s+', ' ', string)
    return string


def format_value(
        value,
        decimals=3,
        width=0,
        decimals_separator='.',
        thousands_separator=',',
        autoint=False,
        symbol='',
        position='after'):
    decimals = max(0, int(decimals))
    width = max(0, int(width))
    value = float(value)

    if autoint and math.floor(value) == value:
        decimals = 0
    if width == 0:
        width = ''

    if thousands_separator:
        formatstr = "{:" + str(width) + ",." + str(decimals) + "f}"
    else:
        formatstr = "{:" + str(width) + "." + str(decimals) + "f}"

    ret = formatstr.format(value)
    ret = ret.replace(',', 'COMMA')
    ret = ret.replace('.', 'DOT')
    ret = ret.replace('COMMA', thousands_separator)
    ret = ret.replace('DOT', decimals_separator)

    if symbol:
        if position == 'after':
            ret = ret + symbol
        else:
            ret = symbol + ret
    return ret
