import csv
import re
from pathlib import Path

import bpy
from mathutils import Vector


ROOT_DIRECTORY = ""
IMPORT_SKELETON = False


BASE_COLOR_SEMANTICS = ("unk_semantic_0x55", "unk_semantic_0x0")
NORMAL_SEMANTICS = ("unk_semantic_0x56", "unk_semantic_0x4")
DETAIL_SEMANTICS = ("unk_semantic_0x18", "unk_semantic_0x3D")
MASK_SEMANTICS = ("unk_semantic_0xE", "unk_semantic_0x8")


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
    path = find_file(root, image_name + ".dds")
    if path is None:
        path = find_file(root, image_name)
    if path is None:
        return None

    image = bpy.data.images.get(path.name)
    if image is not None:
        return image
    try:
        return bpy.data.images.load(str(path), check_existing=True)
    except RuntimeError as exc:
        print("Could not load image {}: {}".format(path, exc))
        return None


def read_image_map(metadata_file):
    result = {}
    with metadata_file.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            semantic = (row.get("semantic") or "").strip()
            image_name = (row.get("image_name") or "").strip()
            if semantic and image_name:
                result[semantic] = image_name
    return result


def find_metadata(asset_folder, material_name):
    material_key = re.sub(r"\.\d{3}$", "", material_name).lower()
    candidates = {material_key}
    metadata_files = list(asset_folder.rglob("*_images.txt"))
    mat_info = asset_folder / "_mat_info"
    if mat_info.is_dir():
        metadata_files.extend(mat_info.rglob("*.txt"))

    for metadata in metadata_files:
        metadata_key = metadata.stem.removesuffix("_images").lower()
        if metadata_key in candidates:
            return metadata

    for metadata in metadata_files:
        metadata_key = metadata.stem.removesuffix("_images").lower()
        if material_key in metadata_key or metadata_key in material_key:
            return metadata
    return None


def image_name_for(image_map, semantic_names):
    for semantic in semantic_names:
        image_name = image_map.get(semantic)
        if image_name and not image_name.startswith("$"):
            return image_name
    return None


def make_material(material, root, image_map):
    material.use_nodes = True
    material.diffuse_color = (0.18, 0.18, 0.18, 1.0)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.inputs["Roughness"].default_value = 0.55
    links.new(shader.outputs["BSDF"], output.inputs["Surface"])

    base_image = load_image(root, image_name_for(image_map, BASE_COLOR_SEMANTICS))
    if base_image:
        base = nodes.new("ShaderNodeTexImage")
        base.image = base_image
        base.label = "COD base color / specular"
        base.image.colorspace_settings.name = "sRGB"
        links.new(base.outputs["Color"], shader.inputs["Base Color"])

    normal_image = load_image(root, image_name_for(image_map, NORMAL_SEMANTICS))
    if normal_image:
        normal = nodes.new("ShaderNodeTexImage")
        normal.image = normal_image
        normal.label = "COD normal / gloss"
        normal.image.colorspace_settings.name = "Non-Color"
        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.inputs["Strength"].default_value = 1.0
        links.new(normal.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])
        gloss_to_roughness = nodes.new("ShaderNodeMath")
        gloss_to_roughness.operation = "SUBTRACT"
        gloss_to_roughness.inputs[0].default_value = 1.0
        links.new(normal.outputs["Alpha"], gloss_to_roughness.inputs[1])
        links.new(gloss_to_roughness.outputs[0], shader.inputs["Roughness"])

    detail_image = load_image(root, image_name_for(image_map, DETAIL_SEMANTICS))
    if detail_image:
        detail = nodes.new("ShaderNodeTexImage")
        detail.image = detail_image
        detail.label = "COD detail mask"
        detail.image.colorspace_settings.name = "Non-Color"

    mask_image = load_image(root, image_name_for(image_map, MASK_SEMANTICS))
    if mask_image:
        mask = nodes.new("ShaderNodeTexImage")
        mask.image = mask_image
        mask.label = "COD material mask"
        mask.image.colorspace_settings.name = "Non-Color"


def import_obj(obj_file):
    before = set(bpy.data.objects)
    try:
        bpy.ops.wm.obj_import(filepath=str(obj_file), use_split_objects=True)
    except AttributeError:
        bpy.ops.import_scene.obj(filepath=str(obj_file), use_split_objects=True)
    return [obj for obj in bpy.data.objects if obj not in before]


def import_model(model_file):
    if model_file.suffix.lower() == ".semodel":
        before = set(bpy.data.objects)
        bpy.ops.import_scene.semodel(filepath=str(model_file))
        return [obj for obj in bpy.data.objects if obj not in before]
    if model_file.suffix.lower() == ".obj":
        return import_obj(model_file)

    before = set(bpy.data.objects)
    if model_file.suffix.lower() == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(model_file))
    elif model_file.suffix.lower() == ".gltf":
        bpy.ops.import_scene.gltf(filepath=str(model_file))
    else:
        raise ValueError("Unsupported model format: {}".format(model_file.suffix))
    return [obj for obj in bpy.data.objects if obj not in before]


def read_smd_skeleton(smd_file):
    nodes = {}
    positions = {}
    in_nodes = False
    in_skeleton = False
    reading_pose = False

    for line in smd_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line == "nodes":
            in_nodes = True
            continue
        if line == "skeleton":
            in_nodes = False
            in_skeleton = True
            continue
        if line == "end":
            if reading_pose:
                break
            in_nodes = False
            continue
        if in_nodes:
            match = re.match(r'(\d+)\s+"(.+)"\s+(-?\d+)$', line)
            if match:
                index, name, parent = match.groups()
                nodes[int(index)] = (name, int(parent))
        elif in_skeleton and line == "time 0":
            reading_pose = True
        elif reading_pose:
            values = line.split()
            if len(values) >= 7:
                index = int(values[0])
                positions[index] = Vector(
                    (float(values[1]), float(values[2]), float(values[3]))
                )

    if not nodes or len(positions) != len(nodes):
        return None

    world_positions = {}

    def world_position(index):
        if index in world_positions:
            return world_positions[index]
        _, parent = nodes[index]
        local = positions[index]
        if parent < 0:
            result = local
        else:
            result = world_position(parent) + local
        world_positions[index] = result
        return result

    return [
        (index, nodes[index][0], nodes[index][1], world_position(index))
        for index in sorted(nodes)
    ]


def create_armature_from_smd(asset_folder, collection):
    smd_files = sorted(asset_folder.glob("*_LOD0.smd"))
    if not smd_files:
        return None
    skeleton = read_smd_skeleton(smd_files[0])
    if not skeleton:
        print("Could not read skeleton: {}".format(smd_files[0]))
        return None

    armature_name = asset_folder.name + "_RIG"
    old_armature = bpy.data.objects.get(armature_name)
    if old_armature:
        bpy.data.objects.remove(old_armature, do_unlink=True)

    armature_data = bpy.data.armatures.new(armature_name)
    armature_object = bpy.data.objects.new(armature_name, armature_data)
    collection.objects.link(armature_object)
    armature_data.display_type = "BBONE"
    armature_object.show_in_front = True

    bpy.context.view_layer.objects.active = armature_object
    armature_object.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones = {}
    children = {}
    for index, name, parent, position in skeleton:
        bone = armature_data.edit_bones.new(name)
        bone.head = position
        bone.tail = position + Vector((0.0, 0.0, 0.1))
        edit_bones[index] = bone
        children.setdefault(parent, []).append(index)

    for index, name, parent, position in skeleton:
        bone = edit_bones[index]
        if parent >= 0 and parent in edit_bones:
            bone.parent = edit_bones[parent]
            bone.use_connect = False
        child_indices = children.get(index, [])
        if child_indices:
            bone.tail = edit_bones[child_indices[0]].head

    bpy.ops.object.mode_set(mode="OBJECT")
    armature_object.select_set(False)

    for _, name, _, position in skeleton:
        if not name.lower().startswith("tag_"):
            continue
        marker = bpy.data.objects.new(name, None)
        marker.empty_display_type = "ARROWS"
        marker.empty_display_size = 0.12
        marker.location = position
        collection.objects.link(marker)

    return armature_object


def process_asset_folder(asset_folder, collection):
    model_files = []
    for extension in (".obj", ".glb", ".gltf"):
        model_files.extend(asset_folder.glob("*_LOD0" + extension))
    model_files = sorted(model_files)
    if not model_files:
        return 0, 0

    imported = import_model(model_files[0])
    if IMPORT_SKELETON:
        create_armature_from_smd(asset_folder, collection)
    for obj in imported:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            metadata = find_metadata(asset_folder, material.name)
            if metadata:
                make_material(material, asset_folder, read_image_map(metadata))
            else:
                print(
                    "Metadata not found for material '{}' in {}".format(
                        material.name, asset_folder.name
                    )
                )

        for old_collection in list(obj.users_collection):
            old_collection.objects.unlink(obj)
        collection.objects.link(obj)
    return len(imported), sum(1 for obj in imported if obj.type == "MESH")


def run_import(root_path):
    root = Path(root_path)
    if not root.is_dir():
        raise ValueError("Selected path is not a directory")

    collection = bpy.data.collections.get("COD MW2 Weapons")
    if collection is None:
        collection = bpy.data.collections.new("COD MW2 Weapons")
        bpy.context.scene.collection.children.link(collection)

    imported_objects = 0
    mesh_objects = 0
    full_folder = root if root.name.lower() == "full" else root / "full"
    if full_folder.is_dir():
        supported = any(
            list(full_folder.glob("*_LOD0" + extension))
            for extension in (".obj", ".glb", ".gltf")
        )
        if not supported:
            cast_files = list(full_folder.glob("*.cast"))
            if cast_files:
                raise ValueError(
                    "The full folder contains CAST only. Install a CAST importer "
                    "or export the model to OBJ, GLB, or GLTF."
                )
        selected_folders = [full_folder]
    elif list(root.glob("*_LOD0.obj")):
        selected_folders = [root]
    else:
        candidates = [
            path for path in root.iterdir()
            if path.is_dir() and list(path.glob("*_LOD0.obj"))
        ]
        prop_candidates = [
            path for path in candidates
            if path.name.lower().startswith("prop_")
        ]
        wpn_candidates = [
            path for path in candidates
            if path.name.lower().startswith("wpn_")
        ]
        selected_folders = sorted(prop_candidates or wpn_candidates or candidates)
    if not selected_folders:
        cast_files = list(root.glob("full/*.cast")) + list(root.glob("*.cast"))
        if cast_files:
            raise ValueError(
                "The full folder contains CAST only. Install a CAST importer "
                "or export the model to OBJ, GLB, or GLTF."
            )
        raise ValueError("No full weapon folder or supported model was found")

    for asset_folder in selected_folders:
        count, meshes = process_asset_folder(asset_folder, collection)
        imported_objects += count
        mesh_objects += meshes

    print(
        "COD MW2 import complete: {} objects, {} mesh objects".format(
            imported_objects, mesh_objects
        )
    )


def apply_materials_to_scene(root_path):
    root = Path(root_path)
    full_folder = root if root.name.lower() == "full" else root / "full"
    if not full_folder.is_dir():
        raise ValueError("Selected weapon folder does not contain a full folder")

    objects = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if not objects:
        objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not objects:
        raise ValueError("No mesh objects found in the current scene")

    material_count = 0
    for obj in objects:
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            metadata = find_metadata(full_folder, material.name)
            if metadata:
                make_material(material, full_folder, read_image_map(metadata))
                material_count += 1
            else:
                print(
                    "Metadata not found for material '{}'".format(material.name)
                )

    if material_count == 0:
        raise ValueError(
            "No matching material metadata found. Check the imported material names."
        )
    print("Applied materials to {} material slots".format(material_count))


def model_file_for(folder):
    for extension in (".semodel", ".obj", ".glb", ".gltf"):
        files = sorted(folder.glob("*_LOD0" + extension))
        if files:
            return files[0]
    return None


def classify_component(folder_name):
    name = folder_name.lower()
    if name.startswith("wpn_"):
        return "Base"
    categories = (
        ("Receiver", ("_rec", "receiver")),
        ("Barrel", ("_bar", "barrel", "barmed")),
        ("Magazine", ("_mag", "xmag", "smag")),
        ("Grip", ("_grip", "_pgrp", "pstlgrp", "grip")),
        ("Stock", ("_stock",)),
        ("Trigger", ("_trig", "trigger")),
        ("Muzzle", ("_mzl", "muzzle")),
        ("Laser", ("_lsr", "laser", "flash")),
        ("Optic", ("optic", "reflex", "scope")),
        ("Hammer", ("hammer",)),
    )
    for category, markers in categories:
        if any(marker in name for marker in markers):
            return category
    return "Other"


def scan_weapon_root(root):
    folders = [
        folder for folder in root.iterdir()
        if folder.is_dir() and model_file_for(folder)
    ]
    slots = {}
    for folder in folders:
        category = classify_component(folder.name)
        slots.setdefault(category, []).append(folder)

    if "Base" not in slots:
        receiver_folders = slots.get("Receiver", [])
        if receiver_folders:
            slots["Base"] = receiver_folders
            del slots["Receiver"]
        else:
            raise ValueError(
                "No base weapon model found. Expected a wpn_* or receiver model."
            )
    return slots


def clear_build_collection():
    collection = bpy.data.collections.get("COD MW2 Build")
    if collection is None:
        return
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
        raise ValueError("No supported model found in {}".format(folder.name))
    imported = import_model(model_file)
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


def find_bone(armature_object, names):
    if armature_object is None or armature_object.type != "ARMATURE":
        return None
    for name in names:
        if name in armature_object.data.bones:
            return name
    return None


def attachment_bone_names(category):
    return {
        "Barrel": ("tag_barrel_attach",),
        "Magazine": ("tag_mag_attach",),
        "Grip": ("tag_pistolgrip_attach", "tag_grip_attach"),
        "Stock": ("tag_stock_attach",),
        "Optic": ("tag_scope", "tag_reflex", "tag_holo", "tag_acog_2"),
        "Muzzle": ("tag_silencer", "tag_muzzle_attach"),
        "Laser": ("tag_laser_attach",),
        "Trigger": ("tag_trigger_attach",),
        "Hammer": ("tag_hammer_attach",),
    }.get(category, ())


def attach_component_armature(component_armature, anchor_armature, category):
    if component_armature is None or anchor_armature is None:
        return False
    source_bone = next(
        (bone for bone in component_armature.data.bones if bone.parent is None),
        None,
    )
    target_name = find_bone(anchor_armature, attachment_bone_names(category))
    if source_bone is None or target_name is None:
        return False

    target_bone = anchor_armature.data.bones[target_name]
    target_world = anchor_armature.matrix_world @ target_bone.matrix_local
    component_armature.matrix_world = (
        target_world @ source_bone.matrix_local.inverted()
    )
    world_matrix = component_armature.matrix_world.copy()
    component_armature.parent = anchor_armature
    component_armature.parent_type = "BONE"
    component_armature.parent_bone = target_name
    component_armature.matrix_world = world_matrix
    return True


def consolidate_armatures(imported_objects, anchor_armature):
    if anchor_armature is None:
        return

    source_armatures = [
        obj for obj in imported_objects
        if obj.type == "ARMATURE" and obj is not anchor_armature
    ]
    existing_bones = {
        bone.name for bone in anchor_armature.data.bones
    }

    bpy.context.view_layer.objects.active = anchor_armature
    anchor_armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        for source in source_armatures:
            to_anchor = anchor_armature.matrix_world.inverted() @ source.matrix_world
            created = {}
            for source_bone in source.data.bones:
                if source_bone.name in existing_bones:
                    continue
                target = anchor_armature.data.edit_bones.new(source_bone.name)
                target.head = to_anchor @ source_bone.head_local
                target.tail = to_anchor @ source_bone.tail_local
                if (target.tail - target.head).length < 0.001:
                    target.tail = target.head + (0.0, 0.05, 0.0)
                created[source_bone.name] = target
                existing_bones.add(source_bone.name)

            for source_bone in source.data.bones:
                target = created.get(source_bone.name)
                if target is None or source_bone.parent is None:
                    continue
                target.parent = anchor_armature.data.edit_bones.get(
                    source_bone.parent.name
                )
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        anchor_armature.select_set(False)

    for obj in imported_objects:
        if obj.type != "MESH":
            continue
        if obj.name.startswith("semodel_bone_vis"):
            continue
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE":
                modifier.object = anchor_armature
        world_matrix = obj.matrix_world.copy()
        obj.parent = anchor_armature
        obj.matrix_world = world_matrix

    for obj in source_armatures:
        bpy.data.objects.remove(obj, do_unlink=True)

    for obj in list(bpy.data.objects):
        if obj.name.startswith("semodel_bone_vis") and obj not in anchor_armature.children:
            if any(
                pose_bone.custom_shape is obj
                for pose_bone in anchor_armature.pose.bones
            ):
                continue
            bpy.data.objects.remove(obj, do_unlink=True)


def build_weapon(root, selected_paths):
    collection = bpy.data.collections.get("COD MW2 Build")
    if collection is None:
        collection = bpy.data.collections.new("COD MW2 Build")
        bpy.context.scene.collection.children.link(collection)
    clear_build_collection()

    weapon_root = bpy.data.objects.new("Weapon_Root", None)
    weapon_root.empty_display_type = "PLAIN_AXES"
    weapon_root.empty_display_size = 0.5
    weapon_root["cod_weapon_root"] = True
    collection.objects.link(weapon_root)

    category_empties = {}
    imported_armatures = {}
    all_imported_objects = []
    total_meshes = 0
    ordered_paths = sorted(
        selected_paths,
        key=lambda item: (0 if item[0] == "Base" else 1 if item[0] == "Receiver" else 2),
    )
    for category, folder in ordered_paths:
        category_empty = category_empties.get(category)
        if category_empty is None:
            category_empty = bpy.data.objects.new(
                "Part_{}".format(category.replace(" ", "_")),
                None,
            )
            category_empty.empty_display_type = "CUBE"
            category_empty.empty_display_size = 0.2
            category_empty.parent = weapon_root
            collection.objects.link(category_empty)
            category_empties[category] = category_empty

        imported = import_component(folder, collection)
        all_imported_objects.extend(imported)
        armature = next(
            (obj for obj in imported if obj.type == "ARMATURE"),
            None,
        )
        if armature:
            imported_armatures[category] = armature
        for obj in imported:
            if obj.type == "MESH" and not armature:
                world_matrix = obj.matrix_world.copy()
                obj.parent = category_empty
                obj.matrix_world = world_matrix
        total_meshes += sum(1 for obj in imported if obj.type == "MESH")

    anchor_armature = imported_armatures.get("Receiver")
    if anchor_armature is None:
        anchor_armature = imported_armatures.get("Base")
    if anchor_armature:
        anchor_armature["cod_weapon_anchor"] = True
        anchor_armature["cod_weapon_root"] = weapon_root.name
        anchor_world = anchor_armature.matrix_world.copy()
        anchor_armature.parent = weapon_root
        anchor_armature.matrix_world = anchor_world
        for category, armature in imported_armatures.items():
            armature["cod_weapon_component"] = True
            armature["cod_weapon_root"] = weapon_root.name
            if armature is anchor_armature or category in {"Base", "Receiver"}:
                continue
            if not attach_component_armature(armature, anchor_armature, category):
                print(
                    "Attachment bone not found for category '{}'".format(category)
                )
        consolidate_armatures(all_imported_objects, anchor_armature)
        anchor_armature.name = "Weapon_RIG"
        anchor_armature["cod_weapon_anchor"] = True
        anchor_armature["cod_weapon_root"] = weapon_root.name
    if total_meshes == 0:
        raise ValueError("Selected components contain no mesh geometry")


class CODMW2_PG_option(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    path: bpy.props.StringProperty()


def slot_items(self, context):
    return [(option.name, option.name, "") for option in self.options]


class CODMW2_PG_slot(bpy.types.PropertyGroup):
    category: bpy.props.StringProperty()
    options: bpy.props.CollectionProperty(type=CODMW2_PG_option)
    choice: bpy.props.EnumProperty(items=slot_items)


class CODMW2_PG_settings(bpy.types.PropertyGroup):
    root_path: bpy.props.StringProperty(subtype="DIR_PATH")
    slots: bpy.props.CollectionProperty(type=CODMW2_PG_slot)


def populate_slots(settings, root):
    settings.slots.clear()
    for category, folders in sorted(scan_weapon_root(root).items()):
        slot = settings.slots.add()
        slot.category = category
        for folder in sorted(folders):
            option = slot.options.add()
            option.name = folder.name
            option.path = str(folder)
        slot.choice = slot.options[0].name


class CODMW2_OT_choose_folder(bpy.types.Operator):
    bl_idname = "cod_mw2.choose_folder"
    bl_label = "Choose weapon folder"
    bl_options = {"REGISTER", "UNDO"}

    directory: bpy.props.StringProperty(
        name="Weapon folder",
        subtype="DIR_PATH",
        default=ROOT_DIRECTORY,
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        root = Path(self.directory)
        try:
            populate_slots(context.scene.cod_mw2_settings, root)
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        context.scene.cod_mw2_settings.root_path = str(root)
        return {"FINISHED"}


class CODMW2_OT_build_weapon(bpy.types.Operator):
    bl_idname = "cod_mw2.build_weapon"
    bl_label = "Build gun"
    bl_description = "Import selected parts and assign materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cod_mw2_settings
        if not settings.slots:
            self.report({"ERROR"}, "Choose a weapon folder first")
            return {"CANCELLED"}
        selected_paths = []
        for slot in settings.slots:
            selected = next(
                (option.path for option in slot.options if option.name == slot.choice),
                None,
            )
            if selected:
                selected_paths.append((slot.category, Path(selected)))
        try:
            build_weapon(Path(settings.root_path), selected_paths)
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Gun built from selected parts")
        return {"FINISHED"}


class CODMW2_PT_import_panel(bpy.types.Panel):
    bl_label = "COD MW2 Gun"
    bl_idname = "CODMW2_PT_import_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COD MW2"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cod_mw2_settings
        layout.label(text="Gun assembly")
        layout.operator(CODMW2_OT_choose_folder.bl_idname, icon="FILE_FOLDER")
        if settings.root_path:
            layout.label(text=Path(settings.root_path).name)
        for slot in settings.slots:
            layout.prop(slot, "choice", text=slot.category)
        if settings.slots:
            layout.separator()
            layout.operator(CODMW2_OT_build_weapon.bl_idname, icon="IMPORT")


CLASSES = (
    CODMW2_PG_option,
    CODMW2_PG_slot,
    CODMW2_PG_settings,
    CODMW2_OT_choose_folder,
    CODMW2_OT_build_weapon,
    CODMW2_PT_import_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cod_mw2_settings = bpy.props.PointerProperty(
        type=CODMW2_PG_settings
    )


def unregister():
    del bpy.types.Scene.cod_mw2_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
