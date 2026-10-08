import bpy
from pathlib import Path


DEFAULT_UNITY_PATH = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def scene_armatures():
    return sorted(
        (obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"),
        key=lambda o: o.name.lower(),
    )


def meshes_of(armature):
    """Collect all mesh objects that belong to the given armature."""
    result = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.name.startswith("semodel_bone_vis"):
            continue
        has_armature_mod = any(
            m.type == "ARMATURE" and m.object is armature
            for m in obj.modifiers
        )
        if has_armature_mod:
            result.append(obj)
            continue
        current = obj.parent
        while current is not None:
            if current is armature:
                result.append(obj)
                break
            current = current.parent
    return result


def objects_for_export(settings):
    """Return all objects (armatures + meshes) to include in the export."""
    primary = bpy.data.objects.get(settings.primary_object)
    if primary is None:
        raise ValueError("No armature selected for export")
    objects = [primary] + meshes_of(primary)
    if settings.export_mode == "ARMS_WEAPON":
        secondary = bpy.data.objects.get(settings.secondary_object)
        if secondary and secondary is not primary:
            objects += [secondary] + meshes_of(secondary)
    return objects


def push_actions_to_nla(armature, action_names):
    """
    Push the selected actions as NLA strips so the FBX exporter
    picks them up via bake_anim_use_nla_strips.
    Returns saved state needed for cleanup.
    """
    if not action_names:
        return None
    if armature.animation_data is None:
        armature.animation_data_create()

    anim_data = armature.animation_data
    saved_action = anim_data.action

    # Mute existing NLA tracks so they don't interfere
    existing_mutes = [(track, track.mute) for track in anim_data.nla_tracks]
    for track, _ in existing_mutes:
        track.mute = True

    # Push each selected action as a new temporary NLA track
    created_tracks = []
    for name in action_names:
        action = bpy.data.actions.get(name)
        if action is None:
            print("Action not found, skipping: {}".format(name))
            continue
        track = anim_data.nla_tracks.new()
        track.name = "CODEXP_" + name
        frame_start = int(action.frame_range[0])
        track.strips.new(name, frame_start, action)
        created_tracks.append(track)

    # Deactivate the active action so NLA drives the rig
    anim_data.action = None

    return (saved_action, existing_mutes, created_tracks)


def restore_nla(armature, nla_state):
    """Remove temporary NLA tracks and restore the armature's original state."""
    if nla_state is None:
        return
    saved_action, existing_mutes, created_tracks = nla_state
    anim_data = armature.animation_data
    if anim_data is None:
        return
    for track in created_tracks:
        try:
            anim_data.nla_tracks.remove(track)
        except ReferenceError:
            pass
    for track, muted in existing_mutes:
        try:
            track.mute = muted
        except ReferenceError:
            pass
    anim_data.action = saved_action


def export_textures(primary, secondary, texture_folder):
    """
    Find all textures used by the exported armatures, convert DDS → PNG,
    and save them to texture_folder. Returns the number of textures saved.
    """
    texture_folder.mkdir(parents=True, exist_ok=True)
    armatures = [primary]
    if secondary is not None:
        armatures.append(secondary)

    all_meshes = []
    for arm in armatures:
        all_meshes.extend(meshes_of(arm))

    scene = bpy.context.scene
    orig_format = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"

    processed = set()
    count = 0
    try:
        for mesh in all_meshes:
            for slot in mesh.material_slots:
                if slot.material is None or not slot.material.use_nodes:
                    continue
                for node in slot.material.node_tree.nodes:
                    if node.type != "TEX_IMAGE" or not node.image:
                        continue
                    image = node.image
                    if image.name in processed:
                        continue
                    processed.add(image.name)
                    stem = Path(image.name).stem
                    out_path = texture_folder / (stem + ".png")
                    try:
                        image.save_render(str(out_path), scene=scene)
                        count += 1
                    except Exception as exc:
                        print("Could not export texture {}: {}".format(image.name, exc))
    finally:
        scene.render.image_settings.file_format = orig_format

    return count


def run_export(settings):
    """Main export routine. Returns (fbx_path, texture_count)."""
    unity_root = Path(bpy.path.abspath(settings.unity_path))
    if not unity_root.is_dir():
        raise ValueError("Unity project folder not found: {}".format(unity_root))

    primary = bpy.data.objects.get(settings.primary_object)
    if primary is None or primary.type != "ARMATURE":
        raise ValueError("Select a valid armature to export")

    secondary = None
    if settings.export_mode == "ARMS_WEAPON":
        secondary = bpy.data.objects.get(settings.secondary_object)

    # Resolve output folder
    category_map = {
        "WEAPON": "Weapons",
        "CHARACTER": "Characters",
        "ARMS": "Arms",
        "ARMS_WEAPON": "Arms",
    }
    category = category_map.get(settings.export_mode, "Weapons")
    export_name = (
        primary.name
        .replace("_RIG", "")
        .replace("Character_Arms", "arms")
        .replace("Character", "character")
        .replace("Weapon", "weapon")
        .lower()
    )
    export_folder = unity_root / "Assets" / "COD" / category / export_name
    export_folder.mkdir(parents=True, exist_ok=True)
    fbx_path = export_folder / (export_name + ".fbx")

    # Build animation list
    selected_anims = [item.name for item in settings.animations if item.selected]

    # Push animations into NLA for export
    nla_state = push_actions_to_nla(primary, selected_anims)

    # Select objects
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects_for_export(settings):
        obj.select_set(True)
    bpy.context.view_layer.objects.active = primary

    try:
        bpy.ops.export_scene.fbx(
            filepath=str(fbx_path),
            use_selection=True,
            # Unity coordinate system
            axis_forward="-Z",
            axis_up="Y",
            bake_space_transform=True,
            # Scale
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_NONE",
            # Mesh
            mesh_smooth_type="FACE",
            use_mesh_modifiers=True,
            # Armature
            add_leaf_bones=False,
            primary_bone_axis="Y",
            secondary_bone_axis="X",
            # Animation
            bake_anim=bool(selected_anims),
            bake_anim_use_all_bones=True,
            bake_anim_use_nla_strips=True,
            bake_anim_use_all_actions=False,
            bake_anim_force_startend_keying=True,
            bake_anim_simplify_factor=0.0,
        )
    finally:
        restore_nla(primary, nla_state)
        bpy.ops.object.select_all(action="DESELECT")

    # Export textures
    tex_count = 0
    if settings.export_textures:
        tex_folder = export_folder / "Textures"
        tex_count = export_textures(primary, secondary, tex_folder)

    return fbx_path, tex_count


# ── property groups ───────────────────────────────────────────────────────────

class CODEXP_PG_anim(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    selected: bpy.props.BoolProperty(default=True)


def primary_object_items(self, context):
    items = [(obj.name, obj.name, "") for obj in scene_armatures()]
    return items or [("NONE", "No armatures in scene", "")]


def secondary_object_items(self, context):
    items = [(obj.name, obj.name, "") for obj in scene_armatures()]
    return items or [("NONE", "No armatures in scene", "")]


class CODEXP_PG_settings(bpy.types.PropertyGroup):
    unity_path: bpy.props.StringProperty(
        name="Unity Project",
        subtype="DIR_PATH",
        default=DEFAULT_UNITY_PATH,
    )
    export_mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("WEAPON", "Weapon", "Export weapon armature and meshes"),
            ("CHARACTER", "Character", "Export character armature and meshes"),
            ("ARMS", "Arms", "Export first-person arms"),
            ("ARMS_WEAPON", "Arms + Weapon", "Export arms and weapon as one FBX"),
        ),
        default="WEAPON",
    )
    primary_object: bpy.props.EnumProperty(
        name="Object",
        items=primary_object_items,
    )
    secondary_object: bpy.props.EnumProperty(
        name="Weapon",
        items=secondary_object_items,
    )
    animations: bpy.props.CollectionProperty(type=CODEXP_PG_anim)
    export_textures: bpy.props.BoolProperty(
        name="Export Textures (PNG)",
        description="Convert and copy all DDS textures to a Textures subfolder",
        default=True,
    )


# ── operators ─────────────────────────────────────────────────────────────────

class CODEXP_OT_choose_folder(bpy.types.Operator):
    bl_idname = "cod_export.choose_folder"
    bl_label = "Set Unity Project Folder"
    bl_options = {"REGISTER"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        path = Path(self.directory)
        if not path.is_dir():
            self.report({"ERROR"}, "Not a valid folder")
            return {"CANCELLED"}
        context.scene.cod_export_settings.unity_path = str(path)
        return {"FINISHED"}


class CODEXP_OT_refresh_anims(bpy.types.Operator):
    bl_idname = "cod_export.refresh_anims"
    bl_label = "Refresh Animations"
    bl_description = "Reload the animation list from all Actions in the current scene"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cod_export_settings
        settings.animations.clear()
        actions = sorted(bpy.data.actions, key=lambda a: a.name.lower())
        for action in actions:
            item = settings.animations.add()
            item.name = action.name
            item.selected = True
        self.report({"INFO"}, "Found {} animations".format(len(settings.animations)))
        return {"FINISHED"}


class CODEXP_OT_export(bpy.types.Operator):
    bl_idname = "cod_export.export"
    bl_label = "Export to Unity"
    bl_description = "Export the selected rig with animations and textures to the Unity project"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cod_export_settings
        if not settings.unity_path:
            self.report({"ERROR"}, "Set the Unity project folder first")
            return {"CANCELLED"}
        try:
            fbx_path, tex_count = run_export(settings)
        except (OSError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        msg = "Exported → {}".format(fbx_path.parent.name)
        if tex_count:
            msg += " + {} textures".format(tex_count)
        self.report({"INFO"}, msg)
        return {"FINISHED"}


# ── panel ─────────────────────────────────────────────────────────────────────

class CODEXP_PT_panel(bpy.types.Panel):
    bl_label = "COD Export"
    bl_idname = "CODEXP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "COD MW2"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cod_export_settings

        # Unity project folder
        row = layout.row(align=True)
        row.operator(CODEXP_OT_choose_folder.bl_idname, icon="FILE_FOLDER", text="Unity Project")
        if settings.unity_path:
            layout.label(text=Path(settings.unity_path).name, icon="CHECKMARK")

        layout.separator()

        # Export mode + object selection
        layout.prop(settings, "export_mode", text="Mode")
        layout.prop(
            settings,
            "primary_object",
            text="Arms" if settings.export_mode == "ARMS_WEAPON" else "Object",
        )
        if settings.export_mode == "ARMS_WEAPON":
            layout.prop(settings, "secondary_object", text="Weapon")

        layout.separator()

        # Animations
        row = layout.row()
        row.label(text="Animations", icon="ACTION")
        row.operator(CODEXP_OT_refresh_anims.bl_idname, text="", icon="FILE_REFRESH")

        if not settings.animations:
            layout.label(text="Press refresh to load animations", icon="INFO")
        else:
            box = layout.box()
            for item in settings.animations:
                box.prop(item, "selected", text=item.name)

        layout.separator()

        # Options
        layout.prop(settings, "export_textures")

        layout.separator()

        # Export button
        layout.operator(CODEXP_OT_export.bl_idname, icon="EXPORT")


# ── register ──────────────────────────────────────────────────────────────────

CLASSES = (
    CODEXP_PG_anim,
    CODEXP_PG_settings,
    CODEXP_OT_choose_folder,
    CODEXP_OT_refresh_anims,
    CODEXP_OT_export,
    CODEXP_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cod_export_settings = bpy.props.PointerProperty(
        type=CODEXP_PG_settings
    )


def unregister():
    del bpy.types.Scene.cod_export_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
