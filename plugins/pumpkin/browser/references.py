import secrets
from dataclasses import dataclass, field
from typing import Any

from .protocol import WorkerErrorCode
from .sdk_protocol import Element, SdkError


_REGISTRY = """() => {
    const ids = new WeakMap();
    let next = 0;
    return {document, id(node) {
        if (!node) return null;
        if (!ids.has(node)) ids.set(node, String(++next));
        return ids.get(node);
    }};
}"""
_COLLECT = """registry => {
    const result = {};
    const visit = root => {
        for (const node of root.querySelectorAll('*')) {
            const tag = node.tagName.toLowerCase();
            if (!['script','style','template','noscript','meta','link','head','title']
                .includes(tag) && !node.closest('head,script,style,template,noscript')) {
                result[registry.id(node)] = node;
            }
            if (node.shadowRoot) visit(node.shadowRoot);
        }
    };
    visit(document);
    return result;
}"""
_PROJECT = """(node, registry) => {
    const norm = value => String(value || '').replace(/\\s+/g, ' ').trim();
    const root = node.getRootNode();
    const related = attribute => norm((node.getAttribute(attribute) || '')
        .split(/\\s+/).map(id => root.getElementById?.(id)?.textContent || '').join(' '));
    const tag = node.tagName.toLowerCase();
    const type = tag === 'input' ? node.type : tag === 'button' ? node.type : tag;
    const labels = [...(node.labels || [])];
    let role = node.getAttribute('role') || '';
    if (!role) {
        if (tag === 'input') {
            role = ({checkbox:'checkbox', radio:'radio', range:'slider',
                number:'spinbutton', button:'button', submit:'button', reset:'button',
                image:'button', hidden:'hidden', file:'file', password:'password'})[type]
                || 'textbox';
        } else {
            role = ({textarea:'textbox', select:node.multiple?'listbox':'combobox',
                button:'button', a:node.hasAttribute('href')?'link':'generic',
                label:'label', fieldset:'group', legend:'legend', output:'status',
                summary:'button', details:'group'})[tag] || (node.isContentEditable ?
                'textbox' : /^h[1-6]$/.test(tag) ? 'heading' : 'generic');
        }
    }
    const group = node.closest('fieldset, [role="group"], [role="radiogroup"]');
    const parent = node.parentElement || node.getRootNode().host;
    const name = related('aria-labelledby') || norm(node.getAttribute('aria-label')) ||
        norm(labels.map(label => label.textContent).join(' ')) ||
        (tag === 'fieldset' ? norm(node.querySelector(':scope > legend')?.textContent) : '') ||
        norm(node.getAttribute('alt')) ||
        (['button','a','label','legend','summary','output'].includes(tag) ||
            ['button','checkbox','radio','heading','option','tab'].includes(role) ?
            norm(node.textContent) : '') ||
        norm(node.getAttribute('placeholder')) || norm(node.getAttribute('title'));
    const style = getComputedStyle(node);
    const visible = style.visibility !== 'hidden' && style.display !== 'none' &&
        node.getClientRects().length > 0;
    const ariaChecked = node.getAttribute('aria-checked');
    const checked = ['checkbox','radio'].includes(type) ? node.checked :
        ariaChecked === null ? null : ariaChecked === 'true';
    const control = tag === 'label' ? node.control : null;
    const secret = ['password','file','hidden'].includes(type) ||
        ['current-password','new-password','one-time-code','cc-number','cc-csc']
            .includes(node.getAttribute('autocomplete'));
    const href = tag === 'a' ? node.href : null;
    const localText = norm([...node.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => n.textContent).join(' ')).slice(0, 2000);
    const semantic = {role, tag, type, name, href, secret, form_name:node.getAttribute('name'),
        labels:labels.map(label => registry.id(label)),
        control:registry.id(control)};
    return {node_id:registry.id(node), connected:node.isConnected &&
            node.ownerDocument === registry.document,
        semantic:JSON.stringify(semantic), tag, type, secret,
        role, name, text:role === 'generic' ? localText :
            norm(node.innerText || node.textContent).slice(0, 2000),
        local_text:localText,
        form_name:norm(node.getAttribute('name')),
        form_id:registry.id(node.form || node.closest('form')),
        interactive:node.hasAttribute('onclick') || node.tabIndex >= 0 ||
            style.cursor === 'pointer' || ['button','link','checkbox','radio',
                'textbox','spinbutton','combobox','listbox','tab'].includes(role),
        valid:!node.willValidate || node.validity.valid,
        value:secret ? null : 'value' in node ? String(node.value) :
            node.isContentEditable ? node.textContent : null,
        checked, selected:tag === 'select' ? [...node.selectedOptions]
            .map(option => option.value) : [],
        visible, disabled:!!node.disabled || node.getAttribute('aria-disabled') === 'true',
        readonly:!!node.readOnly || node.getAttribute('aria-readonly') === 'true',
        required:!!node.required || node.getAttribute('aria-required') === 'true',
        options:tag === 'select' ? [...node.options].map(option => ({
            value:option.value, label:option.label, selected:option.selected,
            disabled:option.disabled})) : [],
        parent_id:registry.id(parent), group_id:registry.id(group),
        control_id:registry.id(control), href,
        description:related('aria-describedby')};
}"""


@dataclass(slots=True)
class NodeReference:
    ref: str
    frame_id: str
    epoch: int
    node_id: str
    handle: Any
    semantic: str
    stale: bool = False


@dataclass(slots=True)
class FrameReferences:
    frame: Any
    frame_id: str
    epoch: int = 0
    registry: Any = None
    by_node: dict[str, str] = field(default_factory=dict)


class BrowserReferences:
    MAX_REFS = 5_000

    def __init__(self) -> None:
        self.frames: dict[Any, FrameReferences] = {}
        self.refs: dict[str, NodeReference] = {}
        self.truncated = False

    async def frame_state(self, frame: Any) -> FrameReferences:
        state = self.frames.get(frame)
        if state is None:
            state = FrameReferences(frame, secrets.token_hex(8))
            self.frames[frame] = state
        same_document = False
        if state.registry is not None:
            try:
                same_document = await state.registry.evaluate(
                    "registry => registry.document === document",
                )
            except Exception:  # noqa: BLE001
                same_document = False
        if not same_document:
            for ref in state.by_node.values():
                self.refs[ref].stale = True
            state.by_node.clear()
            state.epoch += 1
            if state.registry is not None:
                await state.registry.dispose()
            state.registry = await frame.evaluate_handle(_REGISTRY)
        return state

    async def collect(self, frame: Any) -> tuple[Element, ...]:
        state = await self.frame_state(frame)
        collection = await state.registry.evaluate_handle(_COLLECT)
        try:
            projections = await collection.evaluate(
                "(nodes, registry) => Object.fromEntries(Object.entries(nodes)"
                f".map(([id, node]) => [id, ({_PROJECT})(node, registry)]))",
                state.registry,
            )
            for node_id, ref in tuple(state.by_node.items()):
                binding = self.refs[ref]
                projection = projections.get(node_id)
                if not projection or projection["semantic"] != binding.semantic:
                    binding.stale = True
                    state.by_node.pop(node_id)
                    await binding.handle.dispose()
            fresh = await collection.evaluate_handle(
                "(nodes, known) => Object.fromEntries(Object.entries(nodes)"
                ".filter(([id]) => !known.includes(id)))",
                list(state.by_node),
            )
            try:
                handles = await fresh.get_properties()
            finally:
                await fresh.dispose()
            for node_id, handle in handles.items():
                if node_id in state.by_node:
                    await handle.dispose()
                    continue
                if len(self.refs) >= self.MAX_REFS:
                    self.truncated = True
                    await handle.dispose()
                    continue
                projection = projections[node_id]
                if not projection["connected"]:
                    await handle.dispose()
                    continue
                ref = secrets.token_hex(12)
                state.by_node[node_id] = ref
                self.refs[ref] = NodeReference(
                    ref, state.frame_id, state.epoch, node_id, handle,
                    projection["semantic"],
                )
            elements = []
            for node_id, ref in state.by_node.items():
                projection = projections[node_id]
                elements.append(self.element(ref, projection))
            return tuple(elements)
        finally:
            await collection.dispose()

    async def project(self, ref: str) -> dict[str, Any]:
        binding = self.refs.get(ref)
        if binding is None:
            raise SdkError(WorkerErrorCode.REF_NOT_FOUND, "ref does not belong to this run", field="ref")
        state = next(item for item in self.frames.values() if item.frame_id == binding.frame_id)
        if binding.stale or state.frame.is_detached():
            raise SdkError(WorkerErrorCode.REF_STALE, "target document is no longer available", field="ref")
        await self.frame_state(state.frame)
        if binding.epoch != state.epoch:
            binding.stale = True
            raise SdkError(WorkerErrorCode.REF_STALE, "target document was replaced", field="ref")
        try:
            projection = await binding.handle.evaluate(_PROJECT, state.registry)
        except Exception as exc:
            binding.stale = True
            raise SdkError(WorkerErrorCode.REF_STALE, "target node is no longer available", field="ref") from exc
        if not projection["connected"] or projection["semantic"] != binding.semantic:
            binding.stale = True
            state.by_node.pop(binding.node_id, None)
            await binding.handle.dispose()
            raise SdkError(
                WorkerErrorCode.REF_STALE, "target node detached or changed meaning; find again",
                field="ref",
            )
        return projection

    def element(self, ref: str, value: dict[str, Any]) -> Element:
        binding = self.refs[ref]
        state = next(item for item in self.frames.values() if item.frame_id == binding.frame_id)
        return Element(
            ref=ref, frame_id=binding.frame_id, document_epoch=binding.epoch,
            role=value["role"], name=value["name"], text=value["text"],
            value=value["value"], checked=value["checked"],
            selected=tuple(value["selected"]), visible=value["visible"],
            disabled=value["disabled"], readonly=value["readonly"], required=value["required"],
            options=tuple(value["options"]), href=value["href"],
            parent_ref=state.by_node.get(value["parent_id"]),
            fieldset=state.by_node.get(value["group_id"]),
            control_ref=state.by_node.get(value["control_id"]),
            description=value["description"],
            tag=value["tag"], local_text=value["local_text"],
            form_name=value["form_name"], form_ref=state.by_node.get(value["form_id"]),
            input_type=value["type"], interactive=value["interactive"], valid=value["valid"],
        )

    async def within(self, ref: str, parent: str) -> bool:
        await self.project(parent)
        await self.project(ref)
        candidate, scope = self.refs[ref], self.refs[parent]
        if candidate.frame_id != scope.frame_id:
            return False
        return await candidate.handle.evaluate(
            "(node, parent) => { for (let current = node; current; "
            "current = current.parentElement || current.getRootNode().host) "
            "if (current === parent) return true; return false; }",
            scope.handle,
        )

    async def within_many(self, refs: list[str], parent: str) -> set[str]:
        await self.project(parent)
        scope = self.refs[parent]
        state = next(item for item in self.frames.values() if item.frame_id == scope.frame_id)
        candidates = {
            self.refs[ref].node_id: ref for ref in refs
            if self.refs[ref].frame_id == scope.frame_id
        }
        matched = await state.frame.evaluate(
            "args => {"
            f"const nodes = ({_COLLECT})(args.registry);"
            "return args.ids.filter(id => {"
            "for (let node = nodes[id]; node; "
            "node = node.parentElement || node.getRootNode().host) "
            "if (node === args.parent) return true; return false; }); }",
            {"registry": state.registry, "parent": scope.handle, "ids": list(candidates)},
        )
        return {candidates[node_id] for node_id in matched}

    async def close(self) -> None:
        for binding in self.refs.values():
            try:
                await binding.handle.dispose()
            except Exception:  # noqa: BLE001
                binding.stale = True
        self.refs.clear()
        self.frames.clear()
