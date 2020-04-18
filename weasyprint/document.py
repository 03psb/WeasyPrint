"""
    weasyprint.document
    -------------------

"""

import collections
import functools
import io
import math
import shutil

import cairocffi as cairo
import pydyf
from weasyprint.layout import LayoutContext

from . import CSS, __version__
from .css import get_all_computed_styles
from .css.counters import CounterStyle
from .css.targets import TargetCollector
from .draw import draw_page, stacked
from .fonts import FontConfiguration
from .formatting_structure import boxes
from .formatting_structure.build import build_formatting_structure
from .html import W3C_DATE_RE
from .images import get_image_from_uri as original_get_image_from_uri
from .layout import layout_document
from .layout.percentages import percentage
from .logger import LOGGER, PROGRESS_LOGGER
from .pdf import write_pdf_metadata


class Context(pydyf.Stream):
    def __init__(self, alpha_states, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._alpha_states = alpha_states

    def set_alpha(self, alpha, stroke=False):
        if alpha not in self._alpha_states:
            self._alpha_states[alpha] = pydyf.Dictionary(
                {'CA' if stroke else 'ca': alpha})
        self.set_state(alpha)


class Matrix(list):
    def __init__(self, a=1, b=0, c=0, d=1, e=0, f=0, matrix=None):
        if matrix is None:
            matrix = [[a, b, 0], [c, d, 0], [e, f, 1]]
        super().__init__(matrix)

    def __matmul__(self, other):
        assert len(self[0]) == len(other) == len(other[0]) == 3
        m = len(self)
        return Matrix(matrix=[
            [sum(self[i][k] * other[k][j] for k in range(3)) for j in range(3)]
            for i in range(m)])

    @property
    def determinant(self):
        assert len(self) == len(self[0]) == 3
        return (
            self[0][0] * (self[1][1] * self[2][2] - self[1][2] * self[2][1]) -
            self[1][0] * (self[0][1] * self[2][2] - self[0][2] * self[2][1]) +
            self[2][0] * (self[0][1] * self[1][2] - self[0][2] * self[1][1]))

    def transform_point(self, x, y):
        return (Matrix(matrix=[[x, y, 1]]) @ self)[0][:2]


def create_bookmarks(bookmarks, document, parent=None):
    count = len(bookmarks)
    outlines = []
    for bookmark in bookmarks:
        title = bookmark.label
        destination = bookmark.destination
        children = bookmark.children
        state = bookmark.state
        page, x, y = destination

        destination = pydyf.Array((
            document.objects[document.pages['Kids'][page * 3]].reference,
            '/XYZ', x, y, 0))
        outline = pydyf.Dictionary({
            'Title': pydyf.String(title), 'Dest': destination})
        document.add_object(outline)
        children_outlines, children_count = create_bookmarks(
            children, document, parent=outline)
        outline['Count'] = children_count
        if state == 'closed':
            outline['Count'] *= -1
        else:
            count += children_count
        if outlines:
            outline['Prev'] = outlines[-1].reference
            outlines[-1]['Next'] = outline.reference
        if children_outlines:
            outline['First'] = children_outlines[0].reference
            outline['Last'] = children_outlines[-1].reference
        if parent is not None:
            outline['Parent'] = parent.reference
        outlines.append(outline)
    return outlines, count


def _get_matrix(box):
    """Return the matrix for the CSS transforms on this box.

    :returns: a :class:`Matrix` object or :obj:`None`.

    """
    # "Transforms apply to block-level and atomic inline-level elements,
    #  but do not apply to elements which may be split into
    #  multiple inline-level boxes."
    # http://www.w3.org/TR/css3-2d-transforms/#introduction
    if box.style['transform'] and not isinstance(box, boxes.InlineBox):
        border_width = box.border_width()
        border_height = box.border_height()
        origin_x, origin_y = box.style['transform_origin']
        offset_x = percentage(origin_x, border_width)
        offset_y = percentage(origin_y, border_height)
        origin_x = box.border_box_x() + offset_x
        origin_y = box.border_box_y() + offset_y

        matrix = Matrix(e=origin_x, f=origin_y)
        for name, args in box.style['transform']:
            a, b, c, d, e, f = 1, 0, 0, 1, 0, 0
            if name == 'scale':
                a, d = args
            elif name == 'rotate':
                a = d = math.cos(args)
                b = math.sin(args)
                c = -b
            elif name == 'translate':
                e = percentage(args[0], border_width)
                f = percentage(args[1], border_height)
            elif name == 'skew':
                b, c = math.tan(args[1]), math.tan(args[0])
            else:
                assert name == 'matrix'
                a, b, c, d, e, f = args
            matrix = Matrix(a, b, c, d, e, f) @ matrix
        box.transformation_matrix = Matrix(e=-origin_x, f=-origin_y) @ matrix
        return box.transformation_matrix


def rectangle_aabb(matrix, pos_x, pos_y, width, height):
    """Apply a transformation matrix to an axis-aligned rectangle
    and return its axis-aligned bounding box as ``(x, y, width, height)``

    """
    transform_point = matrix.transform_point
    x1, y1 = transform_point(pos_x, pos_y)
    x2, y2 = transform_point(pos_x + width, pos_y)
    x3, y3 = transform_point(pos_x, pos_y + height)
    x4, y4 = transform_point(pos_x + width, pos_y + height)
    box_x1 = min(x1, x2, x3, x4)
    box_y1 = min(y1, y2, y3, y4)
    box_x2 = max(x1, x2, x3, x4)
    box_y2 = max(y1, y2, y3, y4)
    return box_x1, box_y1, box_x2 - box_x1, box_y2 - box_y1


def _gather_links_and_bookmarks(box, bookmarks, links, anchors, matrix):
    transform = _get_matrix(box)
    if transform:
        matrix = transform @ matrix if matrix else transform

    bookmark_label = box.bookmark_label
    if box.style['bookmark_level'] == 'none':
        bookmark_level = None
    else:
        bookmark_level = box.style['bookmark_level']
    state = box.style['bookmark_state']
    link = box.style['link']
    anchor_name = box.style['anchor']
    has_bookmark = bookmark_label and bookmark_level
    # 'link' is inherited but redundant on text boxes
    has_link = link and not isinstance(box, boxes.TextBox)
    # In case of duplicate IDs, only the first is an anchor.
    has_anchor = anchor_name and anchor_name not in anchors
    is_attachment = hasattr(box, 'is_attachment') and box.is_attachment

    if has_bookmark or has_link or has_anchor:
        pos_x, pos_y, width, height = box.hit_area()
        if has_link:
            token_type, link = link
            assert token_type == 'url'
            link_type, target = link
            assert isinstance(target, str)
            if link_type == 'external' and is_attachment:
                link_type = 'attachment'
            if matrix:
                link = (
                    link_type, target, rectangle_aabb(
                        matrix, pos_x, pos_y, pos_x + width, pos_y + height))
            else:
                link = (link_type, target, (
                    pos_x, pos_y, pos_x + width, pos_y + height))
            links.append(link)
        if matrix and (has_bookmark or has_anchor):
            pos_x, pos_y = matrix.transform_point(pos_x, pos_y)
        if has_bookmark:
            bookmarks.append(
                (bookmark_level, bookmark_label, (pos_x, pos_y), state))
        if has_anchor:
            anchors[anchor_name] = pos_x, pos_y

    for child in box.all_children():
        _gather_links_and_bookmarks(child, bookmarks, links, anchors, matrix)


def _w3c_date_to_pdf(string, attr_name):
    """Tranform W3C date to PDF format."""
    if string is None:
        return None
    match = W3C_DATE_RE.match(string)
    if match is None:
        LOGGER.warning('Invalid %s date: %r', attr_name, string)
        return None
    groups = match.groupdict()
    pdf_date = ''
    found = False
    for key in ('second', 'minute', 'hour', 'day', 'month', 'year'):
        if groups[key]:
            found = True
            pdf_date = groups[key] + pdf_date
        elif found:
            pdf_date = '%02i' % (key in ('day', 'month')) + pdf_date
    if groups['hour']:
        assert groups['minute']
        if groups['tz_hour']:
            assert groups['tz_hour'].startswith(('+', '-'))
            assert groups['tz_minute']
            pdf_date += "%+03i'%02i" % (
                int(groups['tz_hour']), int(groups['tz_minute']))
        else:
            pdf_date += 'Z'
    return pdf_date


class Page:
    """Represents a single rendered page.

    .. versionadded:: 0.15

    Should be obtained from :attr:`Document.pages` but not
    instantiated directly.

    """
    def __init__(self, page_box):
        #: The page width, including margins, in CSS pixels.
        self.width = page_box.margin_width()

        #: The page height, including margins, in CSS pixels.
        self.height = page_box.margin_height()

        #: The page bleed widths as a :obj:`dict` with ``'top'``, ``'right'``,
        #: ``'bottom'`` and ``'left'`` as keys, and values in CSS pixels.
        self.bleed = {
            side: page_box.style['bleed_%s' % side].value
            for side in ('top', 'right', 'bottom', 'left')}

        #: The :obj:`list` of ``(bookmark_level, bookmark_label, target)``
        #: :obj:`tuples <tuple>`. ``bookmark_level`` and ``bookmark_label``
        #: are respectively an :obj:`int` and a :obj:`string <str>`, based on
        #: the CSS properties of the same names. ``target`` is an ``(x, y)``
        #: point in CSS pixels from the top-left of the page.
        self.bookmarks = []

        #: The :obj:`list` of ``(link_type, target, rectangle)`` :obj:`tuples
        #: <tuple>`. A ``rectangle`` is ``(x, y, width, height)``, in CSS
        #: pixels from the top-left of the page. ``link_type`` is one of three
        #: strings:
        #:
        #: * ``'external'``: ``target`` is an absolute URL
        #: * ``'internal'``: ``target`` is an anchor name (see
        #:   :attr:`Page.anchors`).
        #:   The anchor might be defined in another page,
        #:   in multiple pages (in which case the first occurence is used),
        #:   or not at all.
        #: * ``'attachment'``: ``target`` is an absolute URL and points
        #:   to a resource to attach to the document.
        self.links = []

        #: The :obj:`dict` mapping each anchor name to its target, an
        #: ``(x, y)`` point in CSS pixels from the top-left of the page.
        self.anchors = {}

        _gather_links_and_bookmarks(
            page_box, self.bookmarks, self.links, self.anchors, matrix=None)
        self._page_box = page_box

    def paint(self, context, left_x=0, top_y=0, scale=1, clip=False):
        """Paint the page into the PDF file.

        :type context: :class:`Context`
        :param context:
            A context object.
        :type left_x: float
        :param left_x:
            X coordinate of the left of the page, in PDF points.
        :type top_y: float
        :param top_y:
            Y coordinate of the top of the page, in PDF points.
        :type scale: float
        :param scale:
            Zoom scale.
        :type clip: bool
        :param clip:
            Whether to clip/cut content outside the page. If false or
            not provided, content can overflow.

        """
        with stacked(context):
            # Make (0, 0) the top-left corner, and make user units CSS pixels:
            context.transform(scale, 0, 0, scale, left_x, top_y)
            if clip:
                width = self.width
                height = self.height
                context.rectangle(0, 0, width, height)
                context.clip()
            draw_page(self._page_box, context)


class DocumentMetadata:
    """Meta-information belonging to a whole :class:`Document`.

    .. versionadded:: 0.20

    New attributes may be added in future versions of WeasyPrint.

    """
    def __init__(self, title=None, authors=None, description=None,
                 keywords=None, generator=None, created=None, modified=None,
                 attachments=None):
        #: The title of the document, as a string or :obj:`None`.
        #: Extracted from the ``<title>`` element in HTML
        #: and written to the ``/Title`` info field in PDF.
        self.title = title
        #: The authors of the document, as a list of strings.
        #: (Defaults to the empty list.)
        #: Extracted from the ``<meta name=author>`` elements in HTML
        #: and written to the ``/Author`` info field in PDF.
        self.authors = authors or []
        #: The description of the document, as a string or :obj:`None`.
        #: Extracted from the ``<meta name=description>`` element in HTML
        #: and written to the ``/Subject`` info field in PDF.
        self.description = description
        #: Keywords associated with the document, as a list of strings.
        #: (Defaults to the empty list.)
        #: Extracted from ``<meta name=keywords>`` elements in HTML
        #: and written to the ``/Keywords`` info field in PDF.
        self.keywords = keywords or []
        #: The name of one of the software packages
        #: used to generate the document, as a string or :obj:`None`.
        #: Extracted from the ``<meta name=generator>`` element in HTML
        #: and written to the ``/Creator`` info field in PDF.
        self.generator = generator
        #: The creation date of the document, as a string or :obj:`None`.
        #: Dates are in one of the six formats specified in
        #: `W3C’s profile of ISO 8601 <http://www.w3.org/TR/NOTE-datetime>`_.
        #: Extracted from the ``<meta name=dcterms.created>`` element in HTML
        #: and written to the ``/CreationDate`` info field in PDF.
        self.created = created
        #: The modification date of the document, as a string or :obj:`None`.
        #: Dates are in one of the six formats specified in
        #: `W3C’s profile of ISO 8601 <http://www.w3.org/TR/NOTE-datetime>`_.
        #: Extracted from the ``<meta name=dcterms.modified>`` element in HTML
        #: and written to the ``/ModDate`` info field in PDF.
        self.modified = modified
        #: File attachments, as a list of tuples of URL and a description or
        #: :obj:`None`. (Defaults to the empty list.)
        #: Extracted from the ``<link rel=attachment>`` elements in HTML
        #: and written to the ``/EmbeddedFiles`` dictionary in PDF.
        #:
        #: .. versionadded:: 0.22
        self.attachments = attachments or []


BookmarkSubtree = collections.namedtuple(
    'BookmarkSubtree', ('label', 'destination', 'children', 'state'))


class Document:
    """A rendered document ready to be painted on a cairo surface.

    Typically obtained from :meth:`HTML.render() <weasyprint.HTML.render>`, but
    can also be instantiated directly with a list of :class:`pages <Page>`, a
    set of :class:`metadata <DocumentMetadata>`, a :func:`url_fetcher
    <weasyprint.default_url_fetcher>` function, and a :class:`font_config
    <weasyprint.fonts.FontConfiguration>`.

    """

    @classmethod
    def _build_layout_context(cls, html, stylesheets,
                              presentational_hints=False, font_config=None,
                              counter_style=None):
        if font_config is None:
            font_config = FontConfiguration()
        if counter_style is None:
            counter_style = CounterStyle()
        target_collector = TargetCollector()
        page_rules = []
        user_stylesheets = []
        for css in stylesheets or []:
            if not hasattr(css, 'matcher'):
                css = CSS(
                    guess=css, media_type=html.media_type,
                    font_config=font_config, counter_style=counter_style)
            user_stylesheets.append(css)
        style_for = get_all_computed_styles(
            html, user_stylesheets, presentational_hints, font_config,
            counter_style, page_rules, target_collector)
        get_image_from_uri = functools.partial(
            original_get_image_from_uri, {}, html.url_fetcher)
        PROGRESS_LOGGER.info('Step 4 - Creating formatting structure')
        context = LayoutContext(
            style_for, get_image_from_uri, font_config, counter_style,
            target_collector)
        return context

    @classmethod
    def _render(cls, html, stylesheets,
                presentational_hints=False, font_config=None,
                counter_style=None):
        if font_config is None:
            font_config = FontConfiguration()

        if counter_style is None:
            counter_style = CounterStyle()

        context = cls._build_layout_context(
            html, stylesheets, presentational_hints, font_config,
            counter_style)

        root_box = build_formatting_structure(
            html.etree_element, context.style_for, context.get_image_from_uri,
            html.base_url, context.target_collector, counter_style)

        page_boxes = layout_document(html, root_box, context)
        rendering = cls(
            [Page(page_box) for page_box in page_boxes],
            DocumentMetadata(**html._get_metadata()),
            html.url_fetcher, font_config)
        return rendering

    def __init__(self, pages, metadata, url_fetcher, font_config):
        #: A list of :class:`Page` objects.
        self.pages = pages
        #: A :class:`DocumentMetadata` object.
        #: Contains information that does not belong to a specific page
        #: but to the whole document.
        self.metadata = metadata
        #: A function or other callable with the same signature as
        #: :func:`default_url_fetcher` called to fetch external resources such
        #: as stylesheets and images.  (See :ref:`url-fetchers`.)
        self.url_fetcher = url_fetcher
        # Keep a reference to font_config to avoid its garbage collection until
        # rendering is destroyed. This is needed as font_config.__del__ removes
        # fonts that may be used when rendering
        self._font_config = font_config
        # PDF alpha states
        self._alpha_states = {}

    def copy(self, pages='all'):
        """Take a subset of the pages.

        .. versionadded:: 0.15

        :type pages: :term:`iterable`
        :param pages:
            An iterable of :class:`Page` objects from :attr:`pages`.
        :return:
            A new :class:`Document` object.

        Examples:

        Write two PDF files for odd-numbered and even-numbered pages::

            # Python lists count from 0 but pages are numbered from 1.
            # [::2] is a slice of even list indexes but odd-numbered pages.
            document.copy(document.pages[::2]).write_pdf('odd_pages.pdf')
            document.copy(document.pages[1::2]).write_pdf('even_pages.pdf')

        Write each page to a numbred PNG file::

            for i, page in enumerate(document.pages):
                document.copy(page).write_png('page_%s.png' % i)

        Combine multiple documents into one PDF file,
        using metadata from the first::

            all_pages = [p for doc in documents for p in doc.pages]
            documents[0].copy(all_pages).write_pdf('combined.pdf')

        """
        if pages == 'all':
            pages = self.pages
        elif not isinstance(pages, list):
            pages = list(pages)
        return type(self)(
            pages, self.metadata, self.url_fetcher, self._font_config)

    def resolve_links(self):
        """Resolve internal hyperlinks.

        .. versionadded:: 0.15

        Links to a missing anchor are removed with a warning.

        If multiple anchors have the same name, the first one is used.

        :returns:
            A generator yielding lists (one per page) like :attr:`Page.links`,
            except that ``target`` for internal hyperlinks is
            ``(page_number, x, y)`` instead of an anchor name.
            The page number is a 0-based index into the :attr:`pages` list,
            and ``x, y`` are in CSS pixels from the top-left of the page.

        """
        anchors = set()
        paged_anchors = []
        for i, page in enumerate(self.pages):
            paged_anchors.append([])
            for anchor_name, (point_x, point_y) in page.anchors.items():
                if anchor_name not in anchors:
                    paged_anchors[-1].append((anchor_name, point_x, point_y))
                    anchors.add(anchor_name)
        for page in self.pages:
            page_links = []
            for link in page.links:
                link_type, anchor_name, rectangle = link
                if link_type == 'internal':
                    if anchor_name not in anchors:
                        LOGGER.error(
                            'No anchor #%s for internal URI reference',
                            anchor_name)
                    else:
                        page_links.append((link_type, anchor_name, rectangle))
                else:
                    # External link
                    page_links.append(link)
            yield page_links, paged_anchors.pop(0)

    def make_bookmark_tree(self, scale):
        """Make a tree of all bookmarks in the document.

        .. versionadded:: 0.15

        :return: A list of bookmark subtrees.
            A subtree is ``(label, target, children, state)``. ``label`` is
            a string, ``target`` is ``(page_number, x, y)`` like in
            :meth:`resolve_links`, and ``children`` is a
            list of child subtrees.

        """
        root = []
        # At one point in the document, for each "output" depth, how much
        # to add to get the source level (CSS values of bookmark-level).
        # E.g. with <h1> then <h3>, level_shifts == [0, 1]
        # 1 means that <h3> has depth 3 - 1 = 2 in the output.
        skipped_levels = []
        last_by_depth = [root]
        previous_level = 0

        for page_number, page in enumerate(self.pages):
            matrix = Matrix(scale, 0, 0, -scale, 0, page.height * scale)
            for level, label, (point_x, point_y), state in page.bookmarks:
                if level > previous_level:
                    # Example: if the previous bookmark is a <h2>, the next
                    # depth "should" be for <h3>. If now we get a <h6> we’re
                    # skipping two levels: append 6 - 3 - 1 = 2
                    skipped_levels.append(level - previous_level - 1)
                else:
                    temp = level
                    while temp < previous_level:
                        temp += 1 + skipped_levels.pop()
                    if temp > previous_level:
                        # We remove too many "skips", add some back:
                        skipped_levels.append(temp - previous_level - 1)

                previous_level = level
                depth = level - sum(skipped_levels)
                assert depth == len(skipped_levels)
                assert depth >= 1

                children = []
                point_x, point_y = matrix.transform_point(point_x, point_y)
                subtree = BookmarkSubtree(
                    label, (page_number, point_x, point_y), children, state)
                last_by_depth[depth - 1].append(subtree)
                del last_by_depth[depth:]
                last_by_depth.append(children)
        return root

    def add_hyperlinks(self, links, anchors, matrix):
        """Include hyperlinks in current PDF page.

        .. versionadded:: 43


        """
        annots = []
        for link in links:
            link_type, link_target, rectangle = link
            x1, y1 = matrix.transform_point(*rectangle[:2])
            x2, y2 = matrix.transform_point(*rectangle[2:])
            if link_type in ('internal', 'external'):
                annot = pydyf.Dictionary({
                    'Type': '/Annot',
                    'Subtype': '/Link',
                    'Rect': pydyf.Array([x1, y1, x2, y2]),
                    'BS': pydyf.Dictionary({'W': 0}),
                })
                if link_type == 'internal':
                    annot['Dest'] = pydyf.String(link_target)
                else:
                    annot['A'] = pydyf.Dictionary({
                        'Type': '/Action',
                        'S': '/URI',
                        'URI': pydyf.String(link_target),
                    })
                annots.append(annot)

        names = {}
        for anchor in anchors:
            anchor_name, x, y = anchor
            names[anchor_name] = matrix.transform_point(x, y)

        return annots, names

    def write_pdf(self, target=None, zoom=1, attachments=None, finisher=None):
        """Paint the pages in a PDF file, with meta-data.

        PDF files written directly by cairo do not have meta-data such as
        bookmarks/outlines and hyperlinks.

        :type target: str, pathlib.Path or file object
        :param target:
            A filename where the PDF file is generated, a file object, or
            :obj:`None`.
        :type zoom: float
        :param zoom:
            The zoom factor in PDF units per CSS units.  **Warning**:
            All CSS units are affected, including physical units like
            ``cm`` and named sizes like ``A4``.  For values other than
            1, the physical CSS units will thus be "wrong".
        :type attachments: list
        :param attachments: A list of additional file attachments for the
            generated PDF document or :obj:`None`. The list's elements are
            :class:`Attachment` objects, filenames, URLs or file-like objects.
        :param finisher: A finisher function that accepts a PDFFile instance as
            its only parameter can be passed to perform post-processing on the
            PDF right before the trailer is written. The function is then
            responsible for calling the instances finish() function.
        :returns:
            The PDF as :obj:`bytes` if ``target`` is not provided or
            :obj:`None`, otherwise :obj:`None` (the PDF is written to
            ``target``).

        """
        # 0.75 = 72 PDF point per inch / 96 CSS pixel per inch
        scale = zoom * 0.75

        PROGRESS_LOGGER.info('Step 6 - Drawing')

        document = pydyf.PDF()
        paged_links_and_anchors = list(self.resolve_links())
        pdf_names = []
        for page, links_and_anchors in zip(
                self.pages, paged_links_and_anchors):
            links, anchors = links_and_anchors

            # TODO: handle bleed translation
            # with stacked(context):
            #     context.translate(
            #         page.bleed['left'] * scale, page.bleed['top'] * scale)

            page_width = math.floor(scale * (
                page.width + page.bleed['left'] + page.bleed['right']))
            page_height = math.floor(scale * (
                page.height + page.bleed['top'] + page.bleed['bottom']))

            stream = Context(self._alpha_states)
            # Draw from the top-left corner
            stream.transform(1, 0, 0, -1, 0, page_height)
            page.paint(stream, scale=scale)
            document.add_object(stream)

            matrix = Matrix(scale, 0, 0, -scale, 0, page.height * scale)
            annots, names = self.add_hyperlinks(links, anchors, matrix)
            for annot in annots:
                document.add_object(annot)

            resources = pydyf.Dictionary({
                'ProcSet': pydyf.Array(['/PDF', '/Text']),
                'ExtGState': pydyf.Dictionary(self._alpha_states),
            })
            document.add_object(resources)

            pdf_page = pydyf.Dictionary({
                'Type': '/Page',
                'Parent': document.pages.reference,
                'MediaBox': pydyf.Array([0, 0, page_width, page_height]),
                'Contents': stream.reference,
                'Resources': resources.reference,
                'Annots': pydyf.Array([annot.reference for annot in annots]),
            })
            document.add_page(pdf_page)

            for name, (x, y) in names.items():
                pdf_names.append(pydyf.String(name))
                pdf_names.append(
                    pydyf.Array([pdf_page.reference, '/XYZ', x, y, 0]))

        document.catalog['Names'] = pydyf.Dictionary({
            'Dests': pydyf.Dictionary({'Names': pydyf.Array(pdf_names)})})

        PROGRESS_LOGGER.info('Step 7 - Adding PDF metadata')

        # Set document information
        if self.metadata.title:
            document.info['Title'] = pydyf.String(self.metadata.title)
        if self.metadata.authors:
            document.info['Author'] = pydyf.String(
                ', '.join(self.metadata.authors))
        if self.metadata.description:
            document.info['Subject'] = pydyf.String(self.metadata.description)
        if self.metadata.keywords:
            document.info['Keywords'] = pydyf.String(
                ', '.join(self.metadata.keywords))
        if self.metadata.generator:
            document.info['Creator'] = pydyf.String(self.metadata.generator)
        document.info['Producer'] = pydyf.String(f'WeasyPrint {__version__}')
        if self.metadata.created:
            document.info['CreationDate'] = pydyf.String(
                _w3c_date_to_pdf(self.metadata.created, 'created'))
        if self.metadata.modified:
            document.info['ModDate'] = pydyf.String(
                _w3c_date_to_pdf(self.metadata.modified, 'modified'))

        # Set bookmarks
        bookmarks = self.make_bookmark_tree(scale)
        if bookmarks:
            outlines, count = create_bookmarks(bookmarks, document)
            document.catalog['Outlines'] = pydyf.Dictionary({
                'Count': count,
                'First': outlines[0].reference,
                'Last': outlines[-1].reference,
            })

        # Add extra PDF metadata: attachments, embedded files
        attachment_links = [
            [link for link in page_links if link[0] == 'attachment']
            for page_links, page_anchors in paged_links_and_anchors]
        # Write extra PDF metadata only when there is a least one from:
        # - attachments in metadata
        # - attachments as function parameters
        # - attachments as PDF links
        # - finisher function to perform post-processing
        # - bleed boxes
        # TODO: handle this with pydyf
        # condition = (
        #     self.metadata.attachments or
        #     attachments or
        #     any(attachment_links) or
        #     finisher or
        #     any(any(page.bleed.values()) for page in self.pages))
        # if condition:
        #     write_pdf_metadata(
        #         file_obj, scale, self.url_fetcher,
        #         self.metadata.attachments + (attachments or []),
        #         attachment_links, self.pages, finisher)

        file_obj = io.BytesIO()
        document.write(file_obj)

        if target is None:
            return file_obj.getvalue()
        else:
            file_obj.seek(0)
            if hasattr(target, 'write'):
                shutil.copyfileobj(file_obj, target)
            else:
                with open(target, 'wb') as fd:
                    shutil.copyfileobj(file_obj, fd)

    def write_image_surface(self, resolution=96):
        """Render pages on a cairo image surface.

        .. versionadded:: 0.17

        There is no decoration around pages other than those specified in CSS
        with ``@page`` rules. The final image is as wide as the widest page.
        Each page is below the previous one, centered horizontally.

        :type resolution: float
        :param resolution:
            The output resolution in PNG pixels per CSS inch. At 96 dpi
            (the default), PNG pixels match the CSS ``px`` unit.
        :returns:
            A ``(surface, png_width, png_height)`` tuple. ``surface`` is a
            cairo :class:`ImageSurface <cairocffi.ImageSurface>`. ``png_width``
            and ``png_height`` are the size of the final image, in PNG pixels.

        """
        dppx = resolution / 96

        widths = [int(math.ceil(p.width * dppx)) for p in self.pages]
        heights = [int(math.ceil(p.height * dppx)) for p in self.pages]

        max_width = max(widths)
        sum_heights = sum(heights)
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, max_width, sum_heights)
        context = cairo.Context(surface)
        pos_y = 0
        PROGRESS_LOGGER.info('Step 6 - Drawing')
        for page, width, height in zip(self.pages, widths, heights):
            pos_x = (max_width - width) / 2
            page.paint(context, pos_x, pos_y, scale=dppx, clip=True)
            pos_y += height
        return surface, max_width, sum_heights

    def write_png(self, target=None, resolution=96):
        """Paint the pages vertically to a single PNG image.

        There is no decoration around pages other than those specified in CSS
        with ``@page`` rules. The final image is as wide as the widest page.
        Each page is below the previous one, centered horizontally.

        :param target:
            A filename, file-like object, or :obj:`None`.
        :type resolution: float
        :param resolution:
            The output resolution in PNG pixels per CSS inch. At 96 dpi
            (the default), PNG pixels match the CSS ``px`` unit.
        :returns:
            A ``(png_bytes, png_width, png_height)`` tuple. ``png_bytes`` is a
            byte string if ``target`` is :obj:`None`, otherwise :obj:`None`
            (the image is written to ``target``).  ``png_width`` and
            ``png_height`` are the size of the final image, in PNG pixels.

        """
        surface, max_width, sum_heights = self.write_image_surface(resolution)
        if target is None:
            target = io.BytesIO()
            surface.write_to_png(target)
            png_bytes = target.getvalue()
        else:
            surface.write_to_png(target)
            png_bytes = None
        return png_bytes, max_width, sum_heights
