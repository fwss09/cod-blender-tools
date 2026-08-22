import csv
from pathlib import Path

import bpy


DEFAULT_ROOT = ""
COLLECTION_NAME = "COD Camo"
PREVIEW_QUEUE = []
PREVIEW_TIMER_ACTIVE = False
CAMO_MAP_CACHE = {}
IMAGE_INDEX_CACHE = {}
PREVIEW_IMAGE_CACHE = {}
DEBUG = True


def debug(message):
    if DEBUG:
        print("[COD CAMO DEBUG] {}".format(message))


def weapon_items(self, context):
    build = bpy.data.collections.get("COD MW2 Build")
    if build is None:
        return []
    return [
        (obj.name, obj.name, "")
        for obj in sorted(
            (
                obj
                for obj in build.objects
                if obj.type == "ARMATURE"
                and obj.get("cod_weapon_anchor")
            ),
            key=lambda obj: obj.name.lower(),
        )
    ]


def camo_prefix(name):
    parts = name.lower().split("_")
    if parts[0] != "camo":
        return "other"
    return "_".join(parts[:2])


def camo_group_items(self, context):
    groups = sorted({option.group for option in self.camos})
    return [
        (group, group.upper(), "Show {} camouflage".format(group.upper()))
        for group in groups
    ]


def camo_items(self, context):
    return [
        (
            str(index),
            option.name,
            "Apply {}".format(option.name),
            option.preview_icon,
            index,
        )
        for index, option in enumerate(self.camos)
        if not self.camo_group or option.group == self.camo_group
    ]


def find_camo_folders(root):
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and next(path.glob("*_camo_images.txt"), None) is not None
        ),
        key=lambda path: path.name.lower(),
    )


def read_camo_map(folder):
    cache_key = str(folder)
    cached = CAMO_MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    metadata = next(folder.glob("*_camo_images.txt"), None)
    if metadata is None:
        raise ValueError("No *_camo_images.txt found in {}".format(folder))
    with metadata.open("r", encoding="utf-8-sig", newline="") as handle:
        result = {
            (row.get("semantic") or "").strip(): (row.get("image_name") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("semantic") or "").strip()
            and (row.get("image_name") or "").strip()
        }
    CAMO_MAP_CACHE[cache_key] = result
    return result


def find_image(folder, image_name):
    if not image_name or image_name.startswith("$"):
        return None
    index_key = str(folder)
    image_index = IMAGE_INDEX_CACHE.get(index_key)
    if image_index is None:
        image_index = {
            path.name.lower(): path
            for path in folder.rglob("*.dds")
        }
        IMAGE_INDEX_CACHE[index_key] = image_index
    return image_index.get(image_name.lower() + ".dds")


def camo_preview_icon(folder):
    try:
        image_name = read_camo_map(folder).get("unk_semantic_0x0")
        debug("{} -> base texture {}".format(folder.name, image_name))
        image_path = find_image(folder, image_name)
        if image_path is None:
            debug("{} -> DDS not found".format(folder.name))
            return 0
        debug("{} -> {}".format(folder.name, image_path))
        key = str(image_path)
        image = PREVIEW_IMAGE_CACHE.get(key)
        if image is None:
            image = bpy.data.images.load(key, check_existing=True)
            PREVIEW_IMAGE_CACHE[key] = image
        image.preview_ensure()
        debug("{} -> preview icon {}".format(folder.name, image.preview.icon_id))
        return image.preview.icon_id
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        debug("{} -> preview error: {}".format(folder.name, exc))
        return 0


def redraw_ui():
    screen = bpy.context.screen
    if screen is not None:
        for area in screen.areas:
            area.tag_redraw()


def preview_timer():
    global PREVIEW_QUEUE, PREVIEW_TIMER_ACTIVE
    settings = getattr(getattr(bpy.context, "scene", None), "cod_camo_settings", None)
    if settings is None:
        PREVIEW_QUEUE = []
        PREVIEW_TIMER_ACTIVE = False
        return None

    for option in settings.camos:
        if option.path in PREVIEW_QUEUE[:3]:
            option.preview_icon = camo_preview_icon(Path(option.path))
            PREVIEW_QUEUE.remove(option.path)
    redraw_ui()
    if PREVIEW_QUEUE:
        return 0.01
    PREVIEW_TIMER_ACTIVE = False
    return None


def load_group_previews(settings, group):
    global PREVIEW_QUEUE, PREVIEW_TIMER_ACTIVE
    PREVIEW_QUEUE = [
        option.path
        for option in settings.camos
        if option.group == group and option.preview_icon == 0
    ]
    if not PREVIEW_TIMER_ACTIVE:
        PREVIEW_TIMER_ACTIVE = True
        bpy.app.timers.register(preview_timer, first_interval=0.01)


def camo_changed(settings, context):
    if not settings.camos or not settings.weapon:
        return
    index = int(settings.camo_index)
    if index < 0 or index >= len(settings.camos):
        return
    weapon = bpy.data.objects.get(settings.weapon)
    if weapon is None or weapon.type != "ARMATURE":
        return
    try:
        apply_camo(weapon, Path(settings.camos[index].path))
    except (OSError, RuntimeError, ValueError) as exc:
        debug("Could not apply selected camo: {}".format(exc))


def camo_group_changed(settings, context):
    load_group_previews(settings, settings.camo_group)
    for index, option in enumerate(settings.camos):
        if option.group == settings.camo_group:
            settings.camo_index = str(index)
            return


def load_camo_image(folder):
    image_name = read_camo_map(folder).get("unk_semantic_0x0")
    image_path = find_image(folder, image_name)
    if image_path is None:
        raise ValueError("Camo base-color texture was not found")
    try:
        return bpy.data.images.load(str(image_path), check_existing=True)
    except RuntimeError as exc:
        raise ValueError("Could not load camo texture: {}".format(exc)) from exc


def parent_is_weapon(obj, weapon):
    current = obj.parent
    while current is not None:
        if current is weapon or current.name.lower().startswith("weapon_root"):
            return True
        current = current.parent
    return False


def weapon_meshes(weapon):
    result = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.name.startswith("semodel_bone_vis"):
            continue
        has_weapon_modifier = any(
            modifier.type == "ARMATURE" and modifier.object is weapon
            for modifier in obj.modifiers
        )
        if has_weapon_modifier or parent_is_weapon(obj, weapon):
            result.append(obj)
    return result


def apply_camo(weapon, camo_folder):
    image = load_camo_image(camo_folder)
    meshes = weapon_meshes(weapon)
    if not meshes:
        raise ValueError("No mesh objects found for the selected weapon")

    changed = 0
    for mesh in meshes:
        for index, slot in enumerate(mesh.material_slots):
            material = slot.material
            if material is None:
                continue
            if material.users > 1:
                material = material.copy()
                mesh.material_slots[index].material = material
            material.use_nodes = True
            nodes = material.node_tree.nodes
            links = material.node_tree.links
            shader = next(
                (node for node in nodes if node.type == "BSDF_PRINCIPLED"),
                None,
            )
            if shader is None or shader.inputs.get("Base Color") is None:
                continue
            base_color = shader.inputs["Base Color"]
            for node_name in ("COD Camo Wear", "COD Camo Edge Geometry",
                              "COD Camo Edge Mask", "COD Camo Edge Ramp",
                              "COD Camo Wear Amount"):
                old_node = nodes.get(node_name)
                if old_node is not None:
                    nodes.remove(old_node)
            old = nodes.get("COD Camo Texture")
            if old is not None:
                nodes.remove(old)
            camo = nodes.new("ShaderNodeTexImage")
            camo.name = "COD Camo Texture"
            camo.label = "COD camouflage"
            camo.image = image
            camo.image.colorspace_settings.name = "sRGB"
            links.new(camo.outputs["Color"], base_color)
            material["cod_camo"] = camo_folder.name
            changed += 1
    if changed == 0:
        raise ValueError("No Principled materials found on the selected weapon")
    print("Applied camo '{}' to {} material slots".format(camo_folder.name, changed))


class CODCAMO_PG_option(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    path: bpy.props.StringProperty()
    preview_icon: bpy.props.IntProperty()
    group: bpy.props.StringProperty()


class CODCAMO_PG_settings(bpy.types.PropertyGroup):
    root_path: bpy.props.StringProperty(subtype="DIR_PATH", default=DEFAULT_ROOT)
    weapon: bpy.props.EnumProperty(name="Weapon", items=weapon_items)
    camos: bpy.props.CollectionProperty(type=CODCAMO_PG_option)
    camo_group: bpy.props.EnumProperty(items=camo_group_items, update=camo_group_changed)
    camo_index: bpy.props.EnumProperty(items=camo_items, update=camo_changed)


class CODCAMO_OT_choose_folder(bpy.types.Operator):
    bl_idname = "cod_camo.choose_folder"
    bl_label = "Choose camos folder"

    directory: bpy.props.StringProperty(subtype="DIR_PATH", default=DEFAULT_ROOT)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        root = Path(self.directory)
        if not root.is_dir():
            self.report({"ERROR"}, "The selected path is not a folder")
            return {"CANCELLED"}
        settings = context.scene.cod_camo_settings
        settings.root_path = str(root)
        settings.camos.clear()
        for folder in find_camo_folders(root):
            option = settings.camos.add()
            option.name = folder.name
            option.path = str(folder)
            option.group = camo_prefix(folder.name)
        debug("Loaded {} camouflage folders from {}".format(
            len(settings.camos), root
        ))
        if not settings.camos:
            self.report({"ERROR"}, "No camouflage folders were found")
            return {"CANCELLED"}
        settings.camo_group = sorted({option.group for option in settings.camos})[0]
        redraw_ui()
        return {"FINISHED"}


class CODCAMO_PT_panel(bpy.types.Panel):
    bl_label = "Camouflage"
    bl_idname = "CODCAMO_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COD MW2"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cod_camo_settings
        layout.operator(CODCAMO_OT_choose_folder.bl_idname, icon="FILE_FOLDER")
        if settings.root_path:
            layout.label(text=Path(settings.root_path).name)
        layout.prop(settings, "weapon")
        layout.prop(settings, "camo_group", text="Group")
        layout.label(text="Available camos")
        layout.template_icon_view(settings, "camo_index", show_labels=False, scale=6.0)


CLASSES = (
    CODCAMO_PG_option,
    CODCAMO_PG_settings,
    CODCAMO_OT_choose_folder,
    CODCAMO_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cod_camo_settings = bpy.props.PointerProperty(
        type=CODCAMO_PG_settings
    )


def unregister():
    global CAMO_MAP_CACHE, IMAGE_INDEX_CACHE, PREVIEW_IMAGE_CACHE
    del bpy.types.Scene.cod_camo_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    CAMO_MAP_CACHE = {}
    IMAGE_INDEX_CACHE = {}
    PREVIEW_IMAGE_CACHE = {}


if __name__ == "__main__":
    register()
