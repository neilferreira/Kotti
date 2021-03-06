import hashlib
import urllib
from collections import defaultdict
from datetime import datetime

from babel.dates import format_date
from babel.dates import format_datetime
from babel.dates import format_time
from pyramid.decorator import reify
from pyramid.i18n import get_locale_name
from pyramid.i18n import get_localizer
from pyramid.i18n import make_localizer
from pyramid.interfaces import ITranslationDirectories
from pyramid.location import inside
from pyramid.location import lineage
from pyramid.renderers import get_renderer
from pyramid.renderers import render
from pyramid.threadlocal import get_current_registry
from pyramid.threadlocal import get_current_request
from pyramid.view import render_view_to_response
from sqlalchemy import and_
from sqlalchemy import not_
from sqlalchemy import or_
from zope.deprecation.deprecation import deprecate

from kotti import DBSession
from kotti import get_settings
from kotti.events import objectevent_listeners
from kotti.interfaces import INavigationRoot
from kotti.resources import Content
from kotti.resources import Document
from kotti.resources import Tag
from kotti.resources import TagsToContents
from kotti.security import authz_context
from kotti.security import has_permission
from kotti.security import view_permitted
from kotti.views.site_setup import CONTROL_PANEL_LINKS
from kotti.views.slots import slot_events
import inspect


def template_api(context, request, **kwargs):
    return get_settings()['kotti.templates.api'][0](
        context, request, **kwargs)


def render_view(context, request, name='', secure=True):
    with authz_context(context, request):
        response = render_view_to_response(context, request, name, secure)
    if response is not None:
        return response.ubody


def add_renderer_globals(event):
    if event['renderer_name'] != 'json':
        request = event['request']
        api = getattr(request, 'template_api', None)
        if api is None and request is not None:
            api = template_api(event['context'], event['request'])
        event['api'] = api


def is_root(context, request):
    return context is request.root


def get_localizer_for_locale_name(locale_name):
    registry = get_current_registry()
    tdirs = registry.queryUtility(ITranslationDirectories, default=[])
    return make_localizer(locale_name, tdirs)


def translate(*args, **kwargs):
    request = get_current_request()
    if request is None:
        localizer = get_localizer_for_locale_name('en')
    else:
        localizer = get_localizer(request)
    return localizer.translate(*args, **kwargs)


class TemplateStructure(object):
    def __init__(self, html):
        self.html = html

    def __html__(self):
        return self.html
    __unicode__ = __html__

    def __getattr__(self, key):
        return getattr(self.html, key)


class Slots(object):
    def __init__(self, context, request):
        self.context = context
        self.request = request

    def __getattr__(self, name):
        for event_type in slot_events:
            if event_type.name == name:
                break
        else:
            raise AttributeError(name)
        value = []
        event = event_type(self.context, self.request)
        for snippet in objectevent_listeners(event):
            if snippet is not None:
                if isinstance(snippet, list):
                    value.extend(snippet)
                else:
                    value.append(snippet)
        setattr(self, name, value)
        return value


class TemplateAPI(object):
    """This implements the 'api' object that's passed to all
    templates.

    Use dict-access as a shortcut to retrieve template macros from
    templates.
    """
    # Instead of overriding these, consider using the
    # 'kotti.overrides' variable.
    BARE_MASTER = 'kotti:templates/master-bare.pt'
    VIEW_MASTER = 'kotti:templates/view/master.pt'
    EDIT_MASTER = 'kotti:templates/edit/master.pt'
    SITE_SETUP_MASTER = 'kotti:templates/site-setup/master.pt'

    body_css_class = ''

    def __init__(self, context, request, bare=None, **kwargs):
        self.context, self.request = context, request

        if getattr(request, 'template_api', None) is None:
            request.template_api = self

        self.S = get_settings()
        if request.is_xhr and bare is None:
            bare = True  # use bare template that renders just the content area
        self.bare = bare
        self.slots = Slots(context, request)
        self.__dict__.update(kwargs)

    @reify
    def edit_needed(self):
        if 'kotti.fanstatic.edit_needed' in self.S:
            return [r.need() for r in self.S['kotti.fanstatic.edit_needed']]

    @reify
    def view_needed(self):
        if 'kotti.fanstatic.view_needed' in self.S:
            return [r.need() for r in self.S['kotti.fanstatic.view_needed']]

    def macro(self, asset_spec, macro_name='main'):
        if self.bare and asset_spec in (
                self.VIEW_MASTER, self.EDIT_MASTER, self.SITE_SETUP_MASTER):
            asset_spec = self.BARE_MASTER
        return get_renderer(asset_spec).implementation().macros[macro_name]

    @reify
    def site_title(self):
        """
        The site title.

        :result: Value of ``kotti.site_title`` (if specified) or the root
        item's ``title`` attribute.
        :rtype: unicode
        """
        value = get_settings().get('kotti.site_title')
        if not value:
            value = self.root.title
        return value

    @reify
    def page_title(self):
        """
        Title for the current page as used in the ``<head>`` section of the
        default ``master.pt`` template.

        :result: '[Human readable view title ]``context.title`` -
                 :meth:`~TemplateAPI.site_title`''
        :rtype: unicode
        """

        view_title = self.request.view_name.replace('_', ' ').title()
        if view_title:
            view_title += u' '
        view_title += self.context.title
        return u'%s - %s' % (view_title, self.site_title)

    def url(self, context=None, *elements, **kwargs):
        """
        URL construction helper. Just a convenience wrapper for
        :func:`pyramid.request.resource_url` with the same signature.  If
        ``context`` is ``None`` the current context is passed to
        ``resource_url``.
        """

        if context is None:
            context = self.context
        return self.request.resource_url(context, *elements, **kwargs)

    @reify
    def root(self):
        """
        The site root.

        :result: The root object of the site.
        :rtype: :class:`kotti.resources.Node`
        """
        return self.lineage[-1]

    @reify
    def navigation_root(self):
        """
        The root node for the navigation.

        :result: Nearest node in the :meth:`lineage` that provides
                 :class:`kotti.interfaces.INavigationRoot` or :meth:`root` if
                 no node provides that interface.
        :rtype: :class:`kotti.resources.Node`
        """
        for o in self.lineage:
            if INavigationRoot.providedBy(o):
                return o
        return self.root

    @reify
    def lineage(self):
        """
        Lineage from current context to the root node.

        :result: List of nodes.
        :rtype: list of :class:`kotti.resources.Node`
        """
        return list(lineage(self.context))

    @reify
    def breadcrumbs(self):
        """
        List of nodes from the :meth:`navigation_root` to the context.

        :result: List of nodes.
        :rtype: list of :class:`kotti.resources.Node`
        """
        breadcrumbs = self.lineage
        if self.root != self.navigation_root:
            index = breadcrumbs.index(self.navigation_root)
            breadcrumbs = breadcrumbs[:index + 1]
        return reversed(breadcrumbs)

    def has_permission(self, permission, context=None):
        """ Convenience wrapper for :func:`pyramid.security.has_permission`
        with the same signature.  If ``context`` is ``None`` the current
        context is passed to ``has_permission``."""
        if context is None:
            context = self.context
        return has_permission(permission, context, self.request)

    def render_view(self, name='', context=None, request=None, secure=True,
                    bare=True):
        if context is None:
            context = self.context
        if request is None:
            request = self.request

        before = self.bare
        try:
            self.bare = bare
            html = render_view(context, request, name, secure)
        finally:
            self.bare = before
        return TemplateStructure(html)

    def render_template(self, renderer, **kwargs):
        return TemplateStructure(render(renderer, kwargs, self.request))

    def list_children(self, context=None, permission='view'):
        if context is None:
            context = self.context
        children = []
        if hasattr(context, 'values'):
            for child in context.values():
                if (not permission or
                        has_permission(permission, child, self.request)):
                    children.append(child)
        return children

    inside = staticmethod(inside)

    def avatar_url(self, user=None, size="14", default_image='identicon'):
        if user is None:
            user = self.request.user
        email = user.email
        if not email:
            email = user.name
        h = hashlib.md5(email).hexdigest()
        query = {'default': default_image, 'size': str(size)}
        url = 'https://secure.gravatar.com/avatar/%s?%s' % (
            h, urllib.urlencode(query))
        return url

    @reify
    def locale_name(self):
        return get_locale_name(self.request)

    def format_date(self, d, format=None):
        if format is None:
            format = self.S['kotti.date_format']
        return format_date(d, format=format, locale=self.locale_name)

    def format_datetime(self, dt, format=None):
        if format is None:
            format = self.S['kotti.datetime_format']
        if not isinstance(dt, datetime):
            dt = datetime.fromtimestamp(dt)
        return format_datetime(dt, format=format, locale=self.locale_name)

    def format_time(self, t, format=None):
        if format is None:
            format = self.S['kotti.time_format']
        return format_time(t, format=format, locale=self.locale_name)

    def get_type(self, name):
        for class_ in get_settings()['kotti.available_types']:
            if class_.type_info.name == name:
                return class_

    def find_edit_view(self, item):
        view_name = self.request.view_name
        if not view_permitted(item, self.request, view_name):
            view_name = u'edit'
        if not view_permitted(item, self.request, view_name):
            view_name = u''
        return view_name

    @reify
    def edit_links(self):
        if not hasattr(self.context, 'type_info'):
            return []
        return [l for l in self.context.type_info.edit_links
                if l.visible(self.context, self.request)]

    @reify
    def site_setup_links(self):
        return [l for l in CONTROL_PANEL_LINKS
                if l.visible(self.root, self.request)]


@deprecate("'ensure_view_selector' is deprecated as of Kotti 0.8. "
           "There is no replacement.")
def ensure_view_selector(func):  # pragma: no cover
    return func


class NodesTree(object):
    def __init__(self, node, request, item_mapping, item_to_children,
                 permission):
        self._node = node
        self._request = request
        self._item_mapping = item_mapping
        self._item_to_children = item_to_children
        self._permission = permission

    @property
    def __parent__(self):
        if self.parent_id:
            return self._item_mapping[self.parent_id]

    @property
    def children(self):
        return [
            NodesTree(
                child,
                self._request,
                self._item_mapping,
                self._item_to_children,
                self._permission,
            )
            for child in self._item_to_children[self.id]
            if has_permission(self._permission, child, self._request)
        ]

    def _flatten(self, item):
        yield item._node
        for ch in item.children:
            for item in self._flatten(ch):
                yield item

    def tolist(self):
        return list(self._flatten(self))

    def __getattr__(self, name):
        return getattr(self._node, name)


def nodes_tree(request, context=None, permission='view'):
    item_mapping = {}
    item_to_children = defaultdict(lambda: [])
    for node in DBSession.query(Content).with_polymorphic(Content):
        item_mapping[node.id] = node
        if has_permission('view', node, request):
            item_to_children[node.parent_id].append(node)

    for children in item_to_children.values():
        children.sort(key=lambda ch: ch.position)

    if context is None:
        node = item_to_children[None][0]
    else:
        node = context

    return NodesTree(
        node,
        request,
        item_mapping,
        item_to_children,
        permission,
    )


def search_content(search_term, request=None, options=None):
    search_function = get_settings()['kotti.search_content'][0]
    if 'options' in search_function.func_code.co_varnames:
        return search_function(search_term=search_term, request=request,
                               options=options)
    return search_function(search_term=search_term, request=request)


def default_search_content(search_term, request=None, options=None):
    '''
        Supported options:
            types: array of content types to search for
    '''
    if options is None:
        options = {}

    searchstring = u'%%%s%%' % search_term

    # generic_filter can be applied to all Node (and subclassed) objects
    generic_filter = or_(Content.name.like(searchstring),
                         Content.title.like(searchstring),
                         Content.description.like(searchstring))

    query = DBSession.query(Content).filter(generic_filter)
    if 'types' in options:
        query = query.filter(Content.type.in_(options['types']))

    results = query.order_by(Content.title.asc()).all()

    # specific result contain objects matching additional criteria
    # but must not match the generic criteria (because these objects
    # are already in the generic_results)
    document_results = DBSession.query(Document).filter(
        and_(Document.body.like(searchstring),
             not_(generic_filter)))

    for results_set in [content_with_tags([searchstring]),
                        document_results.all()]:
        [results.append(c) for c in results_set if not c in results]

    result_dicts = []

    for result in results:
        if has_permission('view', result, request):
            result_dicts.append(dict(
                name=result.name,
                title=result.title,
                description=result.description,
                path=request.resource_path(result)))

    return result_dicts


def content_with_tags(tag_terms):

    return DBSession.query(Content).join(TagsToContents).join(Tag).filter(
        or_(*[Tag.title.like(tag_term) for tag_term in tag_terms])).all()


def search_content_for_tags(tags, request=None):

    result_dicts = []

    for result in content_with_tags(tags):
        if has_permission('view', result, request):
            result_dicts.append(dict(
                name=result.name,
                title=result.title,
                description=result.description,
                path=request.resource_path(result)))

    return result_dicts
