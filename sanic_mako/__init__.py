import os
import asyncio
import functools
import sys
import pkgutil
from collections.abc import Mapping
from typing import Optional, Iterable, Callable, cast, TypeVar, Any, Coroutine, Tuple, Union

from mypy_extensions import KwArg, VarArg
from sanic import Sanic, Request
from sanic.response import HTTPResponse
from sanic.exceptions import ServerError, SanicException
from mako.template import Template    # type: ignore
from mako.lookup import TemplateLookup    # type: ignore
from mako.exceptions import TemplateLookupException, text_error_template    # type: ignore

__version__ = '0.6.2'

__all__ = ('get_lookup', 'render_template', 'render_template_def', 'render_string', 'SanicMako')

APP_KEY = 'sanic_mako_lookup'
APP_CONTEXT_PROCESSORS_KEY = 'sanic_mako_context_processors'
REQUEST_CONTEXT_KEY = 'sanic_mako_context'

F = TypeVar('F', bound=Callable[..., Any])    # pylint: disable=invalid-name


def get_root_path(import_name: str) -> str:
    mod = sys.modules.get(import_name)
    if mod is not None and hasattr(mod, '__file__'):
        return os.path.dirname(os.path.abspath(mod.__file__))

    loader = pkgutil.get_loader(import_name)
    if loader is None or import_name == '__main__':
        return os.getcwd()

    __import__(import_name)
    mod = sys.modules[import_name]
    filepath = getattr(mod, '__file__', None)

    if filepath is None:
        raise RuntimeError('No root path can be found for the provided '
                           'module "{import_name}".  This can happen because the '
                           'module came from an import hook that does '
                           'not provide file name information or because '
                           'it\'s a namespace package.  In this case '
                           'the root path needs to be explicitly provided.')

    return cast(str, os.path.dirname(os.path.abspath(filepath)))


class TemplateError(RuntimeError):
    """ A template has thrown an error during rendering. """

    def __init__(self, template: Union[Template, SanicException]):
        super().__init__()
        self.einfo = sys.exc_info()
        self.text = text_error_template().render()
        if hasattr(template, 'uri'):
            msg = "Error occurred while rendering template '{0}'"
            msg = msg.format(getattr(template, 'uri'))
        else:
            msg = template.args[0]
        super().__init__(msg)


class SanicMako:
    context_processors: Iterable[Callable[[Request], dict]]

    def __init__(self,
                 app: Optional[Sanic] = None,
                 pkg_path: Optional[str] = None,
                 context_processors: Iterable[Callable[[Request], dict]] = (),
                 app_key: str = APP_KEY) -> None:
        self.app = app

        if app:
            self.init_app(app, pkg_path, context_processors, app_key)

    def init_app(self,
                 app: Sanic,
                 pkg_path: Optional[str] = None,
                 context_processors: Iterable[Callable[[Request], dict]] = (),
                 app_key: str = APP_KEY) -> TemplateLookup:

        if pkg_path is not None and os.path.isdir(pkg_path):
            paths = [pkg_path]
        else:
            paths = [os.path.join(get_root_path(app.name), 'templates')]

        self.context_processors = context_processors

        if context_processors:
            setattr(app.ctx, APP_CONTEXT_PROCESSORS_KEY, context_processors)
            app.register_middleware(context_processors_middleware, "request")

        kwargs = {
            'input_encoding': app.config.get('MAKO_INPUT_ENCODING', 'utf-8'),
            'module_directory': app.config.get('MAKO_MODULE_DIRECTORY', None),
            'collection_size': app.config.get('MAKO_COLLECTION_SIZE', -1),
            'imports': app.config.get('MAKO_IMPORTS', []),
            'filesystem_checks': app.config.get('MAKO_FILESYSTEM_CHECKS', True),
            'default_filters': app.config.get('MAKO_DEFAULT_FILTERS', ['str', 'h']),    # noqa
            'preprocessor': app.config.get('MAKO_PREPROCESSOR', None),
            'strict_undefined': app.config.get('MAKO_STRICT_UNDEFINED', False),
        }

        setattr(app.ctx, app_key, TemplateLookup(directories=paths, **kwargs))
        return getattr(app.ctx, app_key)

    @staticmethod
    def template(
            template_name: str,
            app_key: str = APP_KEY,
            status: int = 200) -> Callable[[F], Callable[[VarArg(Any), KwArg(Any)], Coroutine[Any, Any, HTTPResponse]]]:

        def wrapper(func: F) -> Callable[[VarArg(Any), KwArg(Any)], Coroutine[Any, Any, HTTPResponse]]:

            @functools.wraps(func)
            async def wrapped(*args: Any, **kwargs: Any) -> HTTPResponse:
                if asyncio.iscoroutinefunction(func):
                    coro = func
                else:
                    # noinspection PyDeprecation
                    coro = asyncio.coroutine(func)
                context = await coro(*args, **kwargs)
                request = args[-1]
                response = await render_template(template_name, request, context, app_key=app_key)
                response.status = status
                return response

            return wrapped

        return wrapper


def get_lookup(app: Sanic, app_key: str = APP_KEY) -> TemplateLookup:
    return getattr(app.ctx, app_key)


def get_template_with_context(template_name: str,
                              request: Request,
                              context: Mapping,
                              app_key: str = APP_KEY) -> Tuple[Template, Mapping]:
    lookup = get_lookup(request.app, app_key)

    if lookup is None:
        raise TemplateError(
            ServerError("Template engine is not initialized, "
                        "call sanic_mako.init_app first", status_code=500))
    try:
        template = lookup.get_template(template_name)
    except TemplateLookupException as exc:
        raise TemplateError(ServerError(f"Template '{template_name}' not found", status_code=500)) from exc
    if not isinstance(context, Mapping):
        raise TemplateError(ServerError("context should be mapping, not {type(context)}", status_code=500))
    if getattr(request.ctx, REQUEST_CONTEXT_KEY, None):
        context = dict(getattr(request.ctx, REQUEST_CONTEXT_KEY, None), **context)
    return template, context


async def render_string(template_name: str, request: Request, context: Mapping, *, app_key: str = APP_KEY) -> str:
    template, context = get_template_with_context(template_name, request, context, app_key)
    try:
        text = template.render(request=request, app=request.app, **context)
    except Exception as exc:
        translate = request.app.config.get("MAKO_TRANSLATE_EXCEPTIONS", False)
        if translate:
            template.uri = template_name
            raise TemplateError(template) from exc
        raise Exception from exc

    return cast(str, text)


async def render_template_def(template_name: str,
                              def_name: str,
                              request: Request,
                              context: Mapping,
                              *,
                              app_key: str = APP_KEY) -> str:
    template, context = get_template_with_context(template_name, request, context, app_key)
    try:
        text = template.get_def(def_name).render(app=request.app, **context)
    except Exception as exc:
        translate = request.app.config.get("MAKO_TRANSLATE_EXCEPTIONS", True)
        if translate:
            template.uri = template_name
            raise TemplateError(template) from exc
        raise Exception from exc

    return cast(str, text)


async def render_template(template_name: str,
                          request: Request,
                          context: Mapping,
                          *,
                          content_type: Optional[str] = None,
                          app_key: str = APP_KEY) -> HTTPResponse:
    text = await render_string(template_name, request, context, app_key=app_key)
    if content_type is None:
        content_type = 'text/html'
    return HTTPResponse(text, content_type=content_type)


async def context_processors_middleware(request: Request) -> None:
    request.ctx[REQUEST_CONTEXT_KEY] = {}
    for processor in getattr(request.app.ctx, APP_CONTEXT_PROCESSORS_KEY):
        cast(dict, getattr(request.ctx, REQUEST_CONTEXT_KEY)).update((await processor(request)))
