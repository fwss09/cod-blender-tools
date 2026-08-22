import csv
import re
from pathlib import Path

import bpy


COLLECTION_NAME = "COD Attachments"
DEFAULT_ROOT = ""
ATTACHMENT_BONE_NAMES = {
    "flashlights": ("tag_flashlight", "tag_laser_attach"),
    "lasers": ("tag_laser_attach", "tag_laser"),
    "muzzles": ("tag_muzzle_attach", "tag_silencer"),
    "optics": ("tag_scope", "tag_reflex", "tag_holo", "tag_acog_2"),
    "magazines": ("tag_mag_attach",),
    "grips": ("tag_pistolgrip_attach", "tag_grip_attach"),
    "stocks": ("tag_stock_attach",),
}
BASE_COLOR_SEMANTICS = ("unk_semantic_0x55", "unk_semantic_0x0")
NORMAL_SEMANTICS = ("unk_semantic_0x56", "unk_semantic_0x4")


def find_file(root, filename):
    if not filename:
        return None
    wanted = filename.lower()
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == wanted:
            return path
    return None


def load_image(root, image_name):
    if not image_name or image_name.startswith("$"):
        return None
    path = find_file(root, image_name + ".dds") or find_file(root, image_name)
    if path is None:
        return None
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except RuntimeError as exc:
        print("Could not load image {}: {}".format(path, exc))
        return None


def read_image_map(metadata_file):
    with metadata_file.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            (row.get("semantic") or "").strip(): (row.get("image_name") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("semantic") or "").strip() and (row.get("image_name") or "").strip()
        }


def find_metadata(asset_folder, material_name):
    names = [material_name, re.sub(r"\.\d{3}$", "", material_name)]
    for name in names:
        metadata = asset_folder / (name + "_images.txt")
        if metadata.exists():
            return metadata
    return None


def first_image(image_map, semantics):
    for semantic in semantics:
        image_name = image_map.get(semantic)
        if image_name and not image_name.startswith("$"):
            return image_name
    return None


def make_material(material, asset_folder, image_map):
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.inputs["Roughness"].default_value = 0.55
    links.new(shader.outputs["BSDF"], output.inputs["Surface"])

    base_image = load_image(asset_folder, first_image(image_map, BASE_COLOR_SEMANTICS))
    if base_image:
        base = nodes.new("ShaderNodeTexImage")
        base.image = base_image
        base.image.colorspace_settings.name = "sRGB"
        links.new(base.outputs["Color"], shader.inputs["Base Color"])

    normal_image = load_image(asset_folder, first_image(image_map, NORMAL_SEMANTICS))
    if normal_image:
        normal = nodes.new("ShaderNodeTexImage")
        normal.image = normal_image
        normal.image.colorspace_settings.name = "Non-Color"
        normal_map = nodes.new("ShaderNodeNormalMap")
        links.new(normal.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])


def model_file_for(folder):
    files = sorted(folder.glob("*_LOD0.semodel"))
    return files[0] if files else None


def collection_for(name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(collection)
    return collection


def move_to_collection(objects, collection):
    for obj in objects:
        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        collection.objects.link(obj)


def import_attachment(folder, collection):
    model_file = model_file_for(folder)
    if model_file is None:
        raise ValueError("No *_LOD0.semodel found in {}".format(folder))

    before = set(bpy.data.objects)
    bpy.ops.import_scene.semodel(filepath=str(model_file))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    move_to_collection(imported, collection)

    for obj in imported:
        if obj.type != "MESH" or obj.name.startswith("semodel_bone_vis"):
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            metadata = find_metadata(folder, material.name)
            if metadata:
                make_material(material, folder, read_image_map(metadata))
    return imported


def find_root_bone(armature):
    return next((bone for bone in armature.data.bones if bone.parent is None), None)


def find_target_bone(weapon_armature, category):
    names = ATTACHMENT_BONE_NAMES.get(category.lower(), ())
    return next(
        (name for name in names if weapon_armature.data.bones.get(name)),
        None,
    )


def bone_world_matrix(armature, bone_name):
    pose_bone = armature.pose.bones.get(bone_name)
    if pose_bone is None:
        return None
    return armature.matrix_world @ pose_bone.matrix


def attach_to_weapon(imported, weapon_armature, category):
    attachment_armature = next(
        (obj for obj in imported if obj.type == "ARMATURE"),
        None,
    )
    if attachment_armature is None:
        raise ValueError("The attachment does not contain an armature")

    source_bone = find_root_bone(attachment_armature)
    target_name = find_target_bone(weapon_armature, category)
    if source_bone is None:
        raise ValueError("The attachment armature has no root bone")
    if target_name is None:
        supported = ", ".join(ATTACHMENT_BONE_NAMES.get(category.lower(), ()))
        raise ValueError(
            "No compatible weapon bone found. Expected one of: {}".format(supported)
        )

    bpy.context.view_layer.update()
    target_world = bone_world_matrix(weapon_armature, target_name)
    if target_world is None:
        raise ValueError("Could not evaluate weapon bone {}".format(target_name))
    attachment_armature.matrix_world = (
        target_world @ source_bone.matrix_local.inverted()
    )

    constraint = attachment_armature.constraints.new("CHILD_OF")
    constraint.name = "COD Attachment - {}".format(target_name)
    constraint.target = weapon_armature
    constraint.subtarget = target_name
    constraint.use_location_x = False
    constraint.use_location_y = False
    constraint.use_location_z = False
    constraint.use_rotation_x = False
    constraint.use_rotation_y = False
    constraint.use_rotation_z = False
    constraint.use_scale_x = False
    constraint.use_scale_y = False
    constraint.use_scale_z = False
    constraint.inverse_matrix = (
        target_world.inverted() @ attachment_armature.matrix_world
    )
    attachment_armature["cod_attachment_category"] = category
    attachment_armature["cod_attachment_bone"] = target_name
    return attachment_armature


def attachment_folders(root):
    return sorted(
        {model_file.parent for model_file in root.rglob("*_LOD0.semodel")},
        key=lambda path: str(path).lower(),
    )


def category_folders(root):
    return sorted(path for path in root.iterdir() if path.is_dir())


def weapon_armatures():
    weapon_collection = bpy.data.collections.get("COD MW2 Build")
    if weapon_collection is not None:
        candidates = [
            obj for obj in weapon_collection.objects
            if obj.type == "ARMATURE"
            and obj.get("cod_attachment_category") is None
        ]
    else:
        candidates = [
            obj for obj in bpy.context.scene.objects
            if obj.type == "ARMATURE"
            and obj.get("cod_attachment_category") is None
        ]
    candidates.sort(key=lambda obj: obj.name.lower())

    marked = [obj for obj in candidates if obj.get("cod_weapon_anchor")]
    if marked:
        return marked

    assembled = [
        obj for obj in candidates
        if obj.parent is not None
        and obj.parent.name.lower().startswith("weapon_root")
    ]
    if assembled:
        receiver_rigs = [
            obj for obj in assembled
            if "_rec_" in obj.name.lower() or "_receiver" in obj.name.lower()
        ]
        return receiver_rigs or assembled
    return candidates if len(candidates) == 1 else []


class CODATT_PG_option(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    path: bpy.props.StringProperty()


def enum_options(options):
    return [(option.name, option.name, "") for option in options]


def weapon_items(self, context):
    return [(obj.name, obj.name, "") for obj in weapon_armatures()]


def refresh_attachments(settings):
    settings.attachments.clear()
    if not settings.root_path or not settings.categories:
        return
    category = settings.categories[settings.category_index].path
    folder = Path(settings.root_path) / category
    if not folder.is_dir():
        return
    for path in attachment_folders(folder):
        option = settings.attachments.add()
        option.name = str(path.relative_to(folder))
        option.path = str(path)


def category_changed(settings, context):
    new_index = min(
        settings.category_index,
        max(0, len(settings.categories) - 1),
    )
    if settings.category_index != new_index:
        settings.category_index = new_index
        return
    refresh_attachments(settings)
    settings.attachment_index = 0


def populate_categories(settings, root):
    settings.categories.clear()
    settings.attachments.clear()
    for path in category_folders(root):
        option = settings.categories.add()
        option.name = path.name
        option.path = path.name
    settings.category_index = 0
    settings.attachment_index = 0
    refresh_attachments(settings)


class CODATT_PG_settings(bpy.types.PropertyGroup):
    root_path: bpy.props.StringProperty(subtype="DIR_PATH", default=DEFAULT_ROOT)
    weapon: bpy.props.EnumProperty(name="Weapon", items=weapon_items)
    categories: bpy.props.CollectionProperty(type=CODATT_PG_option)
    category_index: bpy.props.IntProperty(update=category_changed)
    attachments: bpy.props.CollectionProperty(type=CODATT_PG_option)
    attachment_index: bpy.props.IntProperty()


class CODATT_OT_choose_folder(bpy.types.Operator):
    bl_idname = "cod_attachments.choose_folder"
    bl_label = "Choose attachments folder"

    directory: bpy.props.StringProperty(subtype="DIR_PATH", default=DEFAULT_ROOT)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        root = Path(self.directory)
        if not root.is_dir():
            self.report({"ERROR"}, "The selected path is not a folder")
            return {"CANCELLED"}
        context.scene.cod_att_settings.root_path = str(root)
        populate_categories(context.scene.cod_att_settings, root)
        return {"FINISHED"}


class CODATT_OT_attach(bpy.types.Operator):
    bl_idname = "cod_attachments.attach"
    bl_label = "Add attachment"
    bl_description = "Import the selected attachment and constrain it to the weapon"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cod_att_settings
        weapon = bpy.data.objects.get(settings.weapon)
        if weapon is None or weapon.type != "ARMATURE":
            self.report({"ERROR"}, "Choose a weapon armature first")
            return {"CANCELLED"}
        if (
            not settings.root_path
            or not settings.categories
            or not settings.attachments
        ):
            self.report({"ERROR"}, "Choose a folder, category and attachment")
            return {"CANCELLED"}

        category = settings.categories[settings.category_index].path
        attachment = settings.attachments[settings.attachment_index].path
        folder = Path(attachment)
        try:
            imported = import_attachment(folder, collection_for(COLLECTION_NAME))
            attach_to_weapon(imported, weapon, category)
        except (OSError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Attachment added to {}".format(weapon.name))
        return {"FINISHED"}


class CODATT_OT_remove(bpy.types.Operator):
    bl_idname = "cod_attachments.remove"
    bl_label = "Remove selected attachment"

    def execute(self, context):
        selected = [
            obj for obj in context.selected_objects
            if obj.get("cod_attachment_category") and obj.type == "ARMATURE"
        ]
        if not selected:
            self.report({"ERROR"}, "Select an attachment armature")
            return {"CANCELLED"}
        for armature in selected:
            children = list(armature.children)
            bpy.data.objects.remove(armature, do_unlink=True)
            for child in children:
                if child.type == "MESH":
                    bpy.data.objects.remove(child, do_unlink=True)
        self.report({"INFO"}, "Selected attachments removed")
        return {"FINISHED"}


class CODATT_UL_categories(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        layout.label(text=item.name, icon="FILE_FOLDER")


class CODATT_UL_attachments(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        layout.label(text=item.name, icon="OBJECT_DATA")


class CODATT_PT_panel(bpy.types.Panel):
    bl_label = "Attachments"
    bl_idname = "CODATT_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COD MW2"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cod_att_settings
        layout.operator(CODATT_OT_choose_folder.bl_idname, icon="FILE_FOLDER")
        if settings.root_path:
            layout.label(text=Path(settings.root_path).name)
        layout.prop(settings, "weapon")
        layout.label(text="Categories")
        layout.template_list(
            "CODATT_UL_categories",
            "",
            settings,
            "categories",
            settings,
            "category_index",
            rows=3,
        )
        layout.label(text="Available attachments")
        layout.template_list(
            "CODATT_UL_attachments",
            "",
            settings,
            "attachments",
            settings,
            "attachment_index",
            rows=6,
        )
        layout.separator()
        layout.operator(CODATT_OT_attach.bl_idname, icon="CONSTRAINT")
        layout.operator(CODATT_OT_remove.bl_idname, icon="X")


CLASSES = (
    CODATT_PG_option,
    CODATT_PG_settings,
    CODATT_OT_choose_folder,
    CODATT_OT_attach,
    CODATT_OT_remove,
    CODATT_UL_categories,
    CODATT_UL_attachments,
    CODATT_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cod_att_settings = bpy.props.PointerProperty(
        type=CODATT_PG_settings
    )


def unregister():
    del bpy.types.Scene.cod_att_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
