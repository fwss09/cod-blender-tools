import csv
import re
from pathlib import Path

import bpy
from mathutils import Matrix


ROOT_DIRECTORY = ""
COLLECTION_NAME = "COD MW2 Character"
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


def find_metadata(asset_folder, material_name):
    names = [material_name, re.sub(r"\.\d{3}$", "", material_name)]
    for name in names:
        for metadata in (
            asset_folder / (name + "_images.txt"),
            asset_folder / "_mat_info" / (name + ".txt"),
        ):
            if metadata.exists():
                return metadata
    return None


def read_image_map(metadata_file):
    with metadata_file.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            (row.get("semantic") or "").strip(): (row.get("image_name") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("semantic") or "").strip() and (row.get("image_name") or "").strip()
        }


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


def clear_collection(collection):
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def move_to_collection(objects, collection):
    for obj in objects:
        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        collection.objects.link(obj)


def import_component(folder, collection):
    model_file = model_file_for(folder)
    if model_file is None:
        raise ValueError("No *_LOD0.semodel found in {}".format(folder))

    before = set(bpy.data.objects)
    bpy.ops.import_scene.semodel(filepath=str(model_file))
    imported = [obj for obj in bpy.data.objects if obj not in before]
    move_to_collection(imported, collection)

    for obj in imported:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            metadata = find_metadata(folder, material.name)
            if metadata:
                make_material(material, folder, read_image_map(metadata))
            else:
                print(
                    "Metadata not found for material '{}' in {}".format(
                        material.name, folder.name
                    )
                )
    return imported


def find_armature(objects):
    return next((obj for obj in objects if obj.type == "ARMATURE"), None)


def is_real_mesh(obj):
    return obj.type == "MESH" and not obj.name.startswith("semodel_bone_vis")


def align_head_to_body(body_armature, head_armature):
    body_bone = body_armature.data.bones.get("j_spine4")
    head_bone = head_armature.data.bones.get("j_spine4")
    if body_bone is None or head_bone is None:
        raise ValueError("Both body and head must contain j_spine4")

    target_world = body_armature.matrix_world @ body_bone.matrix_local
    source_local = head_bone.matrix_local
    head_armature.matrix_world = target_world @ source_local.inverted()


def merge_head_rig(body_armature, head_armature, head_meshes):
    conversion = body_armature.matrix_world.inverted() @ head_armature.matrix_world
    existing_names = {bone.name for bone in body_armature.data.bones}

    bpy.context.view_layer.objects.active = body_armature
    body_armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        created = {}
        for source_bone in head_armature.data.bones:
            if source_bone.name in existing_names:
                continue
            target = body_armature.data.edit_bones.new(source_bone.name)
            target.head = conversion @ source_bone.head_local
            target.tail = conversion @ source_bone.tail_local
            if (target.tail - target.head).length < 0.001:
                target.tail = target.head + (0.0, 0.05, 0.0)
            created[source_bone.name] = target

        for source_bone in head_armature.data.bones:
            if source_bone.name in existing_names:
                continue
            target = created[source_bone.name]
            if source_bone.parent:
                parent_name = source_bone.parent.name
                target.parent = body_armature.data.edit_bones.get(parent_name)
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        body_armature.select_set(False)

    for mesh in head_meshes:
        world_matrix = mesh.matrix_world.copy()
        for modifier in mesh.modifiers:
            if modifier.type == "ARMATURE":
                modifier.object = body_armature
        mesh.parent = body_armature
        mesh.matrix_world = world_matrix


def remove_armature_objects(objects, keep):
    for obj in objects:
        if obj.type == "ARMATURE" and obj is not keep:
            bpy.data.objects.remove(obj, do_unlink=True)
        elif obj.name.startswith("semodel_bone_vis"):
            bpy.data.objects.remove(obj, do_unlink=True)


def build_character(root):
    root = Path(root)
    body_folder = next(
        (path for path in root.iterdir() if path.is_dir() and path.name.lower().startswith("body_")),
        None,
    )
    head_folder = next(
        (path for path in root.iterdir() if path.is_dir() and path.name.lower().startswith("head_")),
        None,
    )
    if body_folder is None or head_folder is None:
        raise ValueError("Folders starting with body_ and head_ are required")

    collection = collection_for(COLLECTION_NAME)
    clear_collection(collection)
    body_objects = import_component(body_folder, collection)
    head_objects = import_component(head_folder, collection)
    body_armature = find_armature(body_objects)
    head_armature = find_armature(head_objects)
    if body_armature is None or head_armature is None:
        raise ValueError("Body and head must contain an armature")

    head_meshes = [obj for obj in head_objects if is_real_mesh(obj)]
    align_head_to_body(body_armature, head_armature)
    merge_head_rig(body_armature, head_armature, head_meshes)
    remove_armature_objects(head_objects, body_armature)

    body_armature.name = "Character_RIG"
    for obj in body_objects:
        if is_real_mesh(obj):
            obj["cod_character_part"] = "body"
    for obj in head_meshes:
        obj["cod_character_part"] = "head"
    print(
        "Character import complete: {} body meshes, {} head meshes".format(
            sum(obj.type == "MESH" for obj in body_objects),
            len(head_meshes),
        )
    )


def build_arms(root):
    root = Path(root)
    arms_folder = next(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.lower().startswith("mp_vm_arms_")
        ),
        None,
    )
    if arms_folder is None:
        raise ValueError("Folder starting with mp_vm_arms_ was not found")

    collection = collection_for(COLLECTION_NAME)
    clear_collection(collection)
    arms_objects = import_component(arms_folder, collection)
    arms_armature = find_armature(arms_objects)
    if arms_armature is None:
        raise ValueError("The arms model must contain an armature")

    arms_armature.name = "Character_Arms_RIG"
    real_meshes = [obj for obj in arms_objects if is_real_mesh(obj)]
    for obj in real_meshes:
        obj["cod_character_part"] = "arms"
    print("Character arms import complete: {} meshes".format(len(real_meshes)))


class CODMW2CHAR_PG_settings(bpy.types.PropertyGroup):
    root_path: bpy.props.StringProperty(subtype="DIR_PATH", default=ROOT_DIRECTORY)
    import_mode: bpy.props.EnumProperty(
        name="Import mode",
        items=(
            ("FULL", "Full character", "Import body and head"),
            ("ARMS", "Arms only", "Import mp_vm_arms"),
        ),
        default="FULL",
    )


class CODMW2CHAR_OT_choose_folder(bpy.types.Operator):
    bl_idname = "cod_mw2_character.choose_folder"
    bl_label = "Choose character folder"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(
        name="Character folder",
        subtype="DIR_PATH",
        default=ROOT_DIRECTORY,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        root = Path(self.directory)
        if not root.is_dir():
            self.report({"ERROR"}, "The selected path is not a folder")
            return {"CANCELLED"}
        context.scene.cod_mw2_character_settings.root_path = str(root)
        return {"FINISHED"}


class CODMW2CHAR_OT_import(bpy.types.Operator):
    bl_idname = "cod_mw2_character.import_character"
    bl_label = "Import character"
    bl_description = "Import body and head into one rig"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            settings = context.scene.cod_mw2_character_settings
            if settings.import_mode == "ARMS":
                build_arms(settings.root_path)
            else:
                build_character(settings.root_path)
        except (OSError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Character imported into one rig")
        return {"FINISHED"}


class CODMW2CHAR_PT_panel(bpy.types.Panel):
    bl_label = "COD MW2 Character"
    bl_idname = "CODMW2CHAR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COD MW2"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cod_mw2_character_settings
        layout.label(text="Character import")
        layout.operator(CODMW2CHAR_OT_choose_folder.bl_idname, icon="FILE_FOLDER")
        if settings.root_path:
            layout.label(text=Path(settings.root_path).name)
        layout.prop(settings, "import_mode", text="Import")
        layout.operator(CODMW2CHAR_OT_import.bl_idname, icon="IMPORT")


CLASSES = (
    CODMW2CHAR_PG_settings,
    CODMW2CHAR_OT_choose_folder,
    CODMW2CHAR_OT_import,
    CODMW2CHAR_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cod_mw2_character_settings = bpy.props.PointerProperty(
        type=CODMW2CHAR_PG_settings
    )


def unregister():
    del bpy.types.Scene.cod_mw2_character_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
