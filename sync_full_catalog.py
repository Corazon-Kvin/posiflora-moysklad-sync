# =============================================================================
#  ABANDONED STEEL MILL / REACTOR SHAFT — MULTI-TIER DUEL ARENA GENERATOR
#  Unreal Engine 5 — Python Editor Scripting Plugin
#  ВСЕ ПОВЕРХНОСТИ СТРОГО ГОРИЗОНТАЛЬНЫ (Pitch = 0.0, Roll = 0.0)
# =============================================================================

import unreal
import math

# -----------------------------------------------------------------------------
# ГЛОБАЛЬНЫЕ КОНСТАНТЫ
# -----------------------------------------------------------------------------
FOLDER_NAME = "Generated_Arena"
CUBE_ASSET_PATH = '/Engine/BasicShapes/Cube.Cube'
CYLINDER_ASSET_PATH = '/Engine/BasicShapes/Cylinder.Cylinder'

EDITOR_ACTOR_SUBSYSTEM = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

CUBE_MESH = unreal.EditorAssetLibrary.load_asset(CUBE_ASSET_PATH)
CYLINDER_MESH = unreal.EditorAssetLibrary.load_asset(CYLINDER_ASSET_PATH)

# Эргономика персонажа (1 unit = 1 cm)
JUMP_STEP = 48.0          # высота шага (45-50 см)
MANTLE_SHELF = 145.0      # высота уступа под мантлинг (120-150 см)
PLATE_THICKNESS = 0.2     # толщина перекрытий (20 см)
CATWALK_THICKNESS = 0.2
TOP_THICKNESS = 0.25

SPAWNED_ACTORS = []


# -----------------------------------------------------------------------------
# БАЗОВЫЕ ХЕЛПЕРЫ
# -----------------------------------------------------------------------------
def make_flat_rotator(yaw):
    """Единственный разрешённый ротатор: Pitch и Roll всегда 0.0."""
    return unreal.Rotator(roll=0.0, pitch=0.0, yaw=float(yaw))


def spawn_mesh_actor(mesh_asset, label, location, scale, yaw=0.0):
    """Спавнит StaticMeshActor, назначает меш, коллизию, ярлык и папку."""
    actor = EDITOR_ACTOR_SUBSYSTEM.spawn_actor_from_class(
        unreal.StaticMeshActor,
        location,
        make_flat_rotator(yaw)
    )
    if actor is None:
        return None

    mesh_component = actor.get_editor_property("static_mesh_component")

    try:
        mesh_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    except Exception:
        pass

    mesh_component.set_editor_property("static_mesh", mesh_asset)
    actor.set_actor_scale3d(scale)

    # Стандартная блочная коллизия
    try:
        mesh_component.set_collision_profile_name("BlockAll")
    except Exception:
        pass
    try:
        mesh_component.set_collision_enabled(unreal.CollisionEnabled.QUERY_AND_PHYSICS)
    except Exception:
        pass
    try:
        mesh_component.set_editor_property("cast_shadow", True)
    except Exception:
        pass

    try:
        mesh_component.set_editor_property("mobility", unreal.ComponentMobility.STATIC)
    except Exception:
        pass

    actor.set_actor_label(label)
    actor.set_folder_path(unreal.Name(FOLDER_NAME))
    SPAWNED_ACTORS.append(actor)
    return actor


def spawn_plate(label, x, y, top_z, scale_x, scale_y, scale_z=PLATE_THICKNESS, yaw=0.0):
    """Горизонтальная плита. top_z — координата ВЕРХНЕЙ (ходибельной) поверхности."""
    center_z = float(top_z) - (float(scale_z) * 100.0) * 0.5
    return spawn_mesh_actor(
        CUBE_MESH, label,
        unreal.Vector(float(x), float(y), center_z),
        unreal.Vector(float(scale_x), float(scale_y), float(scale_z)),
        yaw
    )


def spawn_box_on_ground(label, x, y, base_z, scale_x, scale_y, scale_z, yaw=0.0):
    """Объёмный блок, стоящий НА поверхности base_z (укрытия, колонны, бортики)."""
    center_z = float(base_z) + (float(scale_z) * 100.0) * 0.5
    return spawn_mesh_actor(
        CUBE_MESH, label,
        unreal.Vector(float(x), float(y), center_z),
        unreal.Vector(float(scale_x), float(scale_y), float(scale_z)),
        yaw
    )


def spawn_beam(label, x, y, center_z, scale_x, scale_y, scale_z, yaw=0.0):
    """Балка / элемент каркаса, позиционируемый по центру."""
    return spawn_mesh_actor(
        CUBE_MESH, label,
        unreal.Vector(float(x), float(y), float(center_z)),
        unreal.Vector(float(scale_x), float(scale_y), float(scale_z)),
        yaw
    )


def spawn_cylinder(label, x, y, center_z, scale_x, scale_y, scale_z, yaw=0.0):
    return spawn_mesh_actor(
        CYLINDER_MESH, label,
        unreal.Vector(float(x), float(y), float(center_z)),
        unreal.Vector(float(scale_x), float(scale_y), float(scale_z)),
        yaw
    )


def spawn_player_start(label, x, y, z, yaw):
    actor = EDITOR_ACTOR_SUBSYSTEM.spawn_actor_from_class(
        unreal.PlayerStart,
        unreal.Vector(float(x), float(y), float(z)),
        make_flat_rotator(yaw)
    )
    if actor is None:
        return None
    actor.set_actor_label(label)
    actor.set_folder_path(unreal.Name(FOLDER_NAME))
    SPAWNED_ACTORS.append(actor)
    return actor


# -----------------------------------------------------------------------------
# 0. ОЧИСТКА СЦЕНЫ + ОПРЕДЕЛЕНИЕ ВЫСОТЫ ПРИВЯЗКИ
# -----------------------------------------------------------------------------
def resolve_target_z_and_cleanup():
    all_actors = EDITOR_ACTOR_SUBSYSTEM.get_all_level_actors()

    target_z = None
    doomed = []

    # --- Сначала читаем высоту у ВНЕШНИХ (не сгенерированных) акторов ---
    external_actors = []
    for actor in all_actors:
        if actor is None:
            continue
        try:
            folder = str(actor.get_folder_path())
        except Exception:
            folder = ""
        if folder == FOLDER_NAME:
            doomed.append(actor)
            continue
        external_actors.append(actor)

    # Приоритет 1: существующий PlayerStart
    for actor in external_actors:
        if isinstance(actor, unreal.PlayerStart):
            target_z = float(actor.get_actor_location().z)
            break

    # Приоритет 2: актор с ярлыком Floor / Platform / Base
    if target_z is None:
        for actor in external_actors:
            try:
                label = str(actor.get_actor_label())
            except Exception:
                continue
            low = label.lower()
            if ("floor" in low) or ("platform" in low) or ("base" in low):
                target_z = float(actor.get_actor_location().z)
                break

    # Приоритет 3: фолбэк
    if target_z is None:
        target_z = 250.0
        unreal.log_warning("[ARENA] Опорные акторы не найдены. target_z = 250.0")
    else:
        unreal.log("[ARENA] Определён target_z = {0}".format(target_z))

    # Все PlayerStart подлежат удалению (будут созданы заново)
    for actor in external_actors:
        if isinstance(actor, unreal.PlayerStart):
            doomed.append(actor)

    removed = 0
    for actor in doomed:
        try:
            EDITOR_ACTOR_SUBSYSTEM.destroy_actor(actor)
            removed += 1
        except Exception:
            pass

    unreal.log("[ARENA] Удалено старых акторов: {0}".format(removed))
    return target_z


# -----------------------------------------------------------------------------
# ЯРУС 1: НИЖНИЙ ЦЕХ
# -----------------------------------------------------------------------------
def build_tier_1_foundry(z0):
    # --- Поднимающаяся лава (визуальное дно шахты) ---
    spawn_cylinder("Lava_Surface_Поверхность_Лавы", 0.0, 0.0, z0 - 520.0, 60.0, 60.0, 0.6)
    spawn_cylinder("Lava_Shaft_Стена_Шахты", 0.0, 0.0, z0 - 260.0, 62.0, 62.0, 5.0)

    # --- Основная круглая база (верхняя поверхность ровно на z0) ---
    spawn_cylinder("Base_Floor_Основной_Пол_Цеха", 0.0, 0.0, z0 - 20.0, 32.0, 32.0, 0.4)

    # --- Бортик по периметру нижнего цеха (24 сегмента) ---
    rim_radius = 1580.0
    for i in range(24):
        angle_deg = i * 15.0
        angle_rad = math.radians(angle_deg)
        px = rim_radius * math.cos(angle_rad)
        py = rim_radius * math.sin(angle_rad)
        spawn_box_on_ground(
            "Rim_Guard_Бортик_Цеха_{0:02d}".format(i + 1),
            px, py, z0, 0.4, 4.6, 1.0, angle_deg
        )

    # --- Центральное ядро реактора (несущая опора + блокировка прямой линии огня) ---
    spawn_cylinder("Reactor_Core_Ядро_Реактора", 0.0, 0.0, z0 + 347.5, 7.0, 7.0, 6.95)
    spawn_cylinder("Reactor_Ring_Кольцо_Ядра_Нижнее", 0.0, 0.0, z0 + 120.0, 8.0, 8.0, 0.5)
    spawn_cylinder("Reactor_Ring_Кольцо_Ядра_Среднее", 0.0, 0.0, z0 + 400.0, 7.8, 7.8, 0.4)

    # --- PlayerStart'ы ---
    spawn_player_start("PlayerStart_A", -1100.0, 0.0, z0 + 40.0, 0.0)
    spawn_player_start("PlayerStart_B", 1100.0, 0.0, z0 + 40.0, 180.0)

    # --- Промышленные укрытия возле спавнов (130-140 см) ---
    for side in (-1.0, 1.0):
        tag = "A" if side < 0.0 else "B"

        spawn_box_on_ground("Cover_Трансформатор_{0}_1".format(tag),
                            side * 820.0, 300.0, z0, 1.6, 2.4, 1.4)
        spawn_box_on_ground("Cover_Трансформатор_{0}_2".format(tag),
                            side * 820.0, -300.0, z0, 1.6, 2.4, 1.4)
        spawn_box_on_ground("Cover_Ящик_{0}_1".format(tag),
                            side * 1260.0, 640.0, z0, 1.3, 1.3, 1.35)
        spawn_box_on_ground("Cover_Ящик_{0}_2".format(tag),
                            side * 1260.0, -640.0, z0, 1.3, 1.3, 1.35)
        spawn_box_on_ground("Cover_Стена_Щитовая_{0}".format(tag),
                            side * 480.0, 0.0, z0, 0.9, 4.2, 1.35)

        # Связка "прыжковый шаг 48 см -> уступ мантлинга 145 см"
        for lateral in (-1.0, 1.0):
            sfx = "N" if lateral > 0.0 else "S"
            spawn_box_on_ground(
                "Jump_Step_Ступень_{0}_{1}".format(tag, sfx),
                side * 700.0, lateral * 1010.0, z0, 1.6, 1.6, JUMP_STEP / 100.0
            )
            spawn_box_on_ground(
                "Mantle_Shelf_Уступ_{0}_{1}".format(tag, sfx),
                side * 960.0, lateral * 1010.0, z0, 1.6, 1.6, MANTLE_SHELF / 100.0
            )


# -----------------------------------------------------------------------------
# КАРКАС ЗАВОДА: КОЛОННЫ, БАЛКИ, КРЫША, МОСТОВОЙ КРАН
# -----------------------------------------------------------------------------
def build_factory_frame(z0):
    column_height_scale = 13.0  # 1300 см
    corners = [(-600.0, -600.0, "SW"), (-600.0, 600.0, "NW"),
               (600.0, 600.0, "NE"), (600.0, -600.0, "SE")]

    for (cx, cy, name) in corners:
        spawn_box_on_ground("Column_Колонна_{0}".format(name),
                            cx, cy, z0, 2.0, 2.0, column_height_scale)
        spawn_box_on_ground("Column_Base_Пята_Колонны_{0}".format(name),
                            cx, cy, z0, 2.8, 2.8, 0.45)

    # --- Связующие балки: нижний пояс (Z + 350) ---
    spawn_beam("Beam_Балка_Нижняя_West", -600.0, 0.0, z0 + 350.0, 0.5, 12.0, 0.6)
    spawn_beam("Beam_Балка_Нижняя_East", 600.0, 0.0, z0 + 350.0, 0.5, 12.0, 0.6)
    spawn_beam("Beam_Балка_Нижняя_North", 0.0, 600.0, z0 + 350.0, 12.0, 0.5, 0.6)
    spawn_beam("Beam_Балка_Нижняя_South", 0.0, -600.0, z0 + 350.0, 12.0, 0.5, 0.6)

    # --- Связующие балки: верхний пояс под краном (Z + 700) ---
    spawn_beam("Beam_Балка_Верхняя_West", -600.0, 0.0, z0 + 700.0, 0.5, 12.0, 0.5)
    spawn_beam("Beam_Балка_Верхняя_East", 600.0, 0.0, z0 + 700.0, 0.5, 12.0, 0.5)
    spawn_beam("Beam_Балка_Верхняя_North", 0.0, 600.0, z0 + 700.0, 12.0, 0.5, 0.5)
    spawn_beam("Beam_Балка_Верхняя_South", 0.0, -600.0, z0 + 700.0, 12.0, 0.5, 0.5)

    # --- Рельсы мостового крана ---
    spawn_beam("Crane_Rail_Рельс_North", 0.0, 600.0, z0 + 1150.0, 12.6, 0.6, 0.5)
    spawn_beam("Crane_Rail_Рельс_South", 0.0, -600.0, z0 + 1150.0, 12.6, 0.6, 0.5)

    # --- Мост крана и подвесной крюк ---
    spawn_beam("Crane_Bridge_Мост_Крана", 0.0, 0.0, z0 + 1075.0, 1.2, 13.0, 0.8)
    spawn_beam("Crane_Trolley_Тележка_Крана", 0.0, 0.0, z0 + 1010.0, 1.8, 1.8, 0.6)
    spawn_beam("Crane_Cable_Трос_Крана", 0.0, 0.0, z0 + 920.0, 0.18, 0.18, 1.2)
    spawn_beam("Crane_Hook_Ковш_Крана", 0.0, 0.0, z0 + 835.0, 1.6, 1.6, 0.5)

    # --- Крыша цеха (перекрытия по верху колонн) ---
    spawn_beam("Roof_Крыша_Балка_West", -600.0, 0.0, z0 + 1280.0, 0.6, 13.0, 0.4)
    spawn_beam("Roof_Крыша_Балка_East", 600.0, 0.0, z0 + 1280.0, 0.6, 13.0, 0.4)
    spawn_beam("Roof_Крыша_Балка_North", 0.0, 600.0, z0 + 1280.0, 13.0, 0.6, 0.4)
    spawn_beam("Roof_Крыша_Балка_South", 0.0, -600.0, z0 + 1280.0, 13.0, 0.6, 0.4)
    spawn_beam("Roof_Крыша_Прогон_C1", 0.0, 200.0, z0 + 1280.0, 12.0, 0.4, 0.3)
    spawn_beam("Roof_Крыша_Прогон_C2", 0.0, -200.0, z0 + 1280.0, 12.0, 0.4, 0.3)


# -----------------------------------------------------------------------------
# ЯРУС 2: МОСТИКИ ТЕХОБСЛУЖИВАНИЯ + ПРЯМЫЕ ЛЕСТНИЧНЫЕ МАРШИ
# -----------------------------------------------------------------------------
def build_tier_2_catwalks(z0):
    catwalk_top = z0 + 288.0
    catwalk_half_len = 325.0   # Scale X 6.5 -> 650 см
    catwalk_half_wid = 120.0   # Scale Y 2.4 -> 240 см

    for sign_y in (1.0, -1.0):
        side_name = "North" if sign_y > 0.0 else "South"
        cy = sign_y * 900.0

        # --- Мостик обслуживания ---
        spawn_plate("Catwalk_Мостик_{0}".format(side_name),
                    0.0, cy, catwalk_top, 6.5, 2.4, CATWALK_THICKNESS)

        # --- Отбойники мостика по внешней и внутренней кромке ---
        spawn_box_on_ground("Catwalk_Rail_Отбойник_{0}_Outer".format(side_name),
                            0.0, cy + sign_y * (catwalk_half_wid - 10.0),
                            catwalk_top, 6.5, 0.2, 0.5)
        spawn_box_on_ground("Catwalk_Rail_Отбойник_{0}_Inner".format(side_name),
                            0.0, cy - sign_y * (catwalk_half_wid - 10.0),
                            catwalk_top, 6.5, 0.2, 0.5)

        # --- Подвесы мостика к балкам каркаса ---
        for hx in (-280.0, 0.0, 280.0):
            spawn_beam("Catwalk_Hanger_Подвес_{0}_{1}".format(side_name, int(hx)),
                       hx, cy, catwalk_top + 120.0, 0.16, 0.16, 2.4)

        # --- Прямые лестничные марши: ровно 6 ступеней с каждой стороны ---
        for sign_x in (1.0, -1.0):
            flight_name = "East" if sign_x > 0.0 else "West"
            for step_index in range(1, 7):
                step_top = z0 + JUMP_STEP * step_index
                # 6-я ступень примыкает к торцу мостика, 1-я — у пола цеха
                step_x = sign_x * (catwalk_half_len + 35.0 + 70.0 * (6 - step_index))

                spawn_plate(
                    "Stair_Ступень_{0}_{1}_{2}".format(side_name, flight_name, step_index),
                    step_x, cy, step_top, 0.7, 2.2, PLATE_THICKNESS
                )

                # Вертикальная стойка-косоур под ступенью (строго вертикальная опора)
                post_height = (JUMP_STEP * step_index) - (PLATE_THICKNESS * 100.0)
                if post_height > 5.0:
                    spawn_beam(
                        "Stair_Post_Опора_{0}_{1}_{2}".format(side_name, flight_name, step_index),
                        step_x, cy, z0 + post_height * 0.5,
                        0.18, 0.18, post_height / 100.0
                    )

            # --- Площадка примыкания лестницы к полу цеха ---
            spawn_plate(
                "Stair_Landing_Площадка_{0}_{1}".format(side_name, flight_name),
                sign_x * (catwalk_half_len + 35.0 + 70.0 * 6 + 110.0), cy,
                z0 + JUMP_STEP, 2.0, 2.4, PLATE_THICKNESS
            )


# -----------------------------------------------------------------------------
# ЯРУС 3: КОНСОЛЬНЫЕ ПЛОЩАДКИ (НЕПРЕРЫВНЫЙ ПАРКУРНЫЙ ПОДЪЁМ, ШАГ 48 СМ)
# -----------------------------------------------------------------------------
def build_tier_3_consoles(z0):
    """
    Две зеркальные спирали консолей. Каждая стартует у торца своего мостика
    (Z + 288) и поднимается ровно по 48 см до Z + 672, откуда следует
    последний шаг 48 см на платформу мостового крана (Z + 720).
    """
    start_angle = 60.0
    angle_step = 24.0
    start_radius = 870.0
    end_radius = 600.0
    count = 8

    chains = [("East", 0.0, "North"), ("West", 180.0, "South")]

    for (chain_name, angle_offset, origin_name) in chains:
        for i in range(count):
            angle_deg = start_angle - angle_step * i + angle_offset
            radius = start_radius - (start_radius - end_radius) * (i / float(count - 1))
            angle_rad = math.radians(angle_deg)

            px = radius * math.cos(angle_rad)
            py = radius * math.sin(angle_rad)
            top_z = z0 + 288.0 + JUMP_STEP * (i + 1)

            # Разворот площадки лицом к центру шахты — строго по Yaw
            plate_yaw = angle_deg + 90.0

            spawn_plate(
                "Console_Консоль_{0}_{1}_От_{2}".format(chain_name, i + 1, origin_name),
                px, py, top_z, 2.0, 2.0, PLATE_THICKNESS, plate_yaw
            )

            # Кронштейн-подвес консоли (вертикальный, без наклона)
            spawn_beam(
                "Console_Bracket_Кронштейн_{0}_{1}".format(chain_name, i + 1),
                px, py, top_z - 90.0, 0.2, 0.2, 1.6, plate_yaw
            )

            # Отбойник по внешней кромке консоли (не мешает прыжку внутрь)
            outer_x = (radius + 90.0) * math.cos(angle_rad)
            outer_y = (radius + 90.0) * math.sin(angle_rad)
            spawn_box_on_ground(
                "Console_Guard_Отбойник_{0}_{1}".format(chain_name, i + 1),
                outer_x, outer_y, top_z, 0.2, 2.0, 0.5, plate_yaw
            )


# -----------------------------------------------------------------------------
# ЯРУС 4: ПЛАТФОРМА МОСТОВОГО КРАНА / ВЕРШИНА РЕАКТОРА
# -----------------------------------------------------------------------------
def build_tier_4_crane_deck(z0):
    deck_top = z0 + 720.0

    # --- Центральная площадка финальной дуэли (1000 x 800 x 25) ---
    spawn_plate("CraneDeck_Платформа_Финальной_Дуэли",
                0.0, 0.0, deck_top, 10.0, 8.0, TOP_THICKNESS)

    # --- Усиливающие рёбра снизу площадки ---
    spawn_beam("CraneDeck_Ребро_Продольное_1", 0.0, 250.0, deck_top - 45.0, 10.0, 0.4, 0.4)
    spawn_beam("CraneDeck_Ребро_Продольное_2", 0.0, -250.0, deck_top - 45.0, 10.0, 0.4, 0.4)
    spawn_beam("CraneDeck_Ребро_Поперечное_1", 350.0, 0.0, deck_top - 45.0, 0.4, 8.0, 0.4)
    spawn_beam("CraneDeck_Ребро_Поперечное_2", -350.0, 0.0, deck_top - 45.0, 0.4, 8.0, 0.4)

    # --- Индустриальные отбойники (высота 50 см, толщина 20 см) ---
    # Проходы оставлены строго напротив точек выхода паркурных спиралей.
    spawn_box_on_ground("CraneDeck_Отбойник_North_1", -220.0, 390.0, deck_top, 5.6, 0.2, 0.5)
    spawn_box_on_ground("CraneDeck_Отбойник_North_2", 405.0, 390.0, deck_top, 1.9, 0.2, 0.5)
    spawn_box_on_ground("CraneDeck_Отбойник_South_1", 220.0, -390.0, deck_top, 5.6, 0.2, 0.5)
    spawn_box_on_ground("CraneDeck_Отбойник_South_2", -405.0, -390.0, deck_top, 1.9, 0.2, 0.5)
    spawn_box_on_ground("CraneDeck_Отбойник_East", 490.0, 0.0, deck_top, 0.2, 8.0, 0.5)
    spawn_box_on_ground("CraneDeck_Отбойник_West", -490.0, 0.0, deck_top, 0.2, 8.0, 0.5)

    # --- 2 стальных контейнера-укрытия на верхнем ринге ---
    spawn_box_on_ground("CraneDeck_Контейнер_Укрытие_1", -260.0, 160.0, deck_top, 2.4, 1.2, 1.4)
    spawn_box_on_ground("CraneDeck_Контейнер_Укрытие_2", 260.0, -160.0, deck_top, 2.4, 1.2, 1.4)

    # --- Прыжковые тумбы 48 см для контрприёмов на верхнем ринге ---
    spawn_box_on_ground("CraneDeck_Тумба_1", 300.0, 250.0, deck_top, 1.4, 1.4, JUMP_STEP / 100.0)
    spawn_box_on_ground("CraneDeck_Тумба_2", -300.0, -250.0, deck_top, 1.4, 1.4, JUMP_STEP / 100.0)

    # --- Венчающее кольцо реактора под платформой ---
    spawn_cylinder("Reactor_Ring_Кольцо_Ядра_Верхнее", 0.0, 0.0, deck_top - 90.0, 7.6, 7.6, 0.4)


# -----------------------------------------------------------------------------
# ГЛАВНАЯ ТОЧКА ВХОДА
# -----------------------------------------------------------------------------
def build_arena():
    if CUBE_MESH is None or CYLINDER_MESH is None:
        unreal.log_error("[ARENA] Не удалось загрузить базовые меши /Engine/BasicShapes. Генерация прервана.")
        return

    del SPAWNED_ACTORS[:]

    target_z = resolve_target_z_and_cleanup()

    build_tier_1_foundry(target_z)
    build_factory_frame(target_z)
    build_tier_2_catwalks(target_z)
    build_tier_3_consoles(target_z)
    build_tier_4_crane_deck(target_z)

    unreal.log("=" * 68)
    unreal.log("[ARENA] ГЕНЕРАЦИЯ ЗАВЕРШЕНА")
    unreal.log("[ARENA] target_z .................. {0}".format(target_z))
    unreal.log("[ARENA] Ярус 1 (пол цеха) ......... Z = {0}".format(target_z))
    unreal.log("[ARENA] Ярус 2 (мостики) .......... Z = {0}".format(target_z + 288.0))
    unreal.log("[ARENA] Ярус 3 (консоли) .......... Z = {0} .. {1}".format(target_z + 336.0, target_z + 672.0))
    unreal.log("[ARENA] Ярус 4 (мостовой кран) .... Z = {0}".format(target_z + 720.0))
    unreal.log("[ARENA] Создано акторов ........... {0}".format(len(SPAWNED_ACTORS)))
    unreal.log("[ARENA] Папка аутлайнера .......... {0}".format(FOLDER_NAME))
    unreal.log("=" * 68)


build_arena()
