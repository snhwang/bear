import * as THREE from 'three';

const PERCH_POINTS = [
    { x: 2, y: 2, height: 0.8 },
    { x: 17, y: 2, height: 1.0 },
    { x: 17, y: 11, height: 0.6 },
];
const VIEW_MODES = ['Normal', 'Dog', 'Cat'];

// Mood color tints applied to pet materials
const MOOD_TINTS = {
    neutral:  null,
    excited:  0xffdd44,
    content:  0x88ff88,
    cautious: 0xaaaacc,
    annoyed:  0xff8888,
    playful:  0xffaaff,
    sleepy:   0x8888cc,
};

let scene, camera, renderer;
let gridPlane, groundMesh;
let dogMesh, catMesh;
let windowMesh;
let stimulusMeshes = new Map();
let scentCircles = new Map();
let scentLine = null;
let perchMarkers = [];
let perchPlatforms = [];
let motionPulses = [];
let pulsePool = [];

let ws = null;
let reconnectDelay = 1000;
let connected = false;

let prevSnapshot = null;
let nextSnapshot = null;
let snapshotReceivedTime = 0;
const SNAPSHOT_INTERVAL = 100;

let currentTool = 'ball';
let viewMode = 0;
let lastActionTime = 0;
const ACTION_COOLDOWN = 500;

let prevPetPositions = {};
let prevStimulusIds = new Set();
let heartSprites = [];
let sparkleSprites = [];
let effectSprites = [];

let cameraOffset = { x: 0, z: 0 };
let cameraZoom = 1;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
let touchState = {
    active: false,
    lastTouches: [],
    isPanning: false,
    isZooming: false
};

// Track thought bubble timers
let thoughtTimers = { dog1: null, cat1: null };
let stimThoughtTimers = {};

// Track previous stimulus effect/thought state for change detection
let prevStimulusState = {};

// Track previous mood/animation for change detection
let prevPetState = {};

// Brain panel state
let brainPanelOpen = false;
let lastDecisionSeq = 0;
let decisionCount = 0;

// Teach / learned panel state
let learnedPanelOpen = false;
let learnedBehaviors = [];  // {id, pet_id, species, content, tags, scope, user_message, ts, triggerCount}
let teachPending = false;

// ---------------------------------------------------------------------------
// Heart / sparkle / effect particle helpers
// ---------------------------------------------------------------------------

function createHeartShape() {
    const shape = new THREE.Shape();
    const s = 0.15;
    shape.moveTo(0, s * 2);
    shape.bezierCurveTo(0, s * 3, -s * 2, s * 3, -s * 2, s * 2);
    shape.bezierCurveTo(-s * 2, s, -s * 2, 0, 0, -s * 2);
    shape.bezierCurveTo(s * 2, 0, s * 2, s, s * 2, s * 2);
    shape.bezierCurveTo(s * 2, s * 3, 0, s * 3, 0, s * 2);
    return shape;
}

function showPetFeedback(position) {
    const heartShape = createHeartShape();
    const heartGeo = new THREE.ShapeGeometry(heartShape);
    const heartMat = new THREE.MeshBasicMaterial({
        color: 0xff4d6d,
        transparent: true,
        opacity: 1,
        side: THREE.DoubleSide
    });
    const heart = new THREE.Mesh(heartGeo, heartMat);
    heart.position.copy(position);
    heart.position.y += 1.5;
    heart.position.x += (Math.random() - 0.5) * 0.5;
    heart.position.z += (Math.random() - 0.5) * 0.5;
    heart.rotation.x = -Math.PI / 4;
    heart.userData.startTime = performance.now();
    heart.userData.startY = heart.position.y;
    heart.userData.wobble = Math.random() * Math.PI * 2;
    scene.add(heart);
    heartSprites.push(heart);

    for (let i = 0; i < 2; i++) {
        setTimeout(() => {
            const extraHeart = new THREE.Mesh(
                new THREE.ShapeGeometry(createHeartShape()),
                new THREE.MeshBasicMaterial({ color: 0xff6b9d, transparent: true, opacity: 0.8, side: THREE.DoubleSide })
            );
            extraHeart.position.copy(position);
            extraHeart.position.y += 1.3;
            extraHeart.position.x += (Math.random() - 0.5) * 0.8;
            extraHeart.position.z += (Math.random() - 0.5) * 0.8;
            extraHeart.rotation.x = -Math.PI / 4;
            extraHeart.scale.setScalar(0.7);
            extraHeart.userData.startTime = performance.now();
            extraHeart.userData.startY = extraHeart.position.y;
            extraHeart.userData.wobble = Math.random() * Math.PI * 2;
            scene.add(extraHeart);
            heartSprites.push(extraHeart);
        }, i * 150);
    }
}

function showSparkles(position) {
    for (let i = 0; i < 5; i++) {
        const geo = new THREE.SphereGeometry(0.05, 8, 8);
        const mat = new THREE.MeshBasicMaterial({ color: 0xffd700, transparent: true, opacity: 1 });
        const spark = new THREE.Mesh(geo, mat);
        spark.position.copy(position);
        spark.position.y += 1.0 + Math.random() * 0.5;
        spark.position.x += (Math.random() - 0.5) * 0.6;
        spark.position.z += (Math.random() - 0.5) * 0.6;
        spark.userData.startTime = performance.now();
        spark.userData.startY = spark.position.y;
        spark.userData.vx = (Math.random() - 0.5) * 0.01;
        spark.userData.vz = (Math.random() - 0.5) * 0.01;
        scene.add(spark);
        sparkleSprites.push(spark);
    }
}

function showEffectBubble(position, effectType) {
    // Create a floating text sprite for effects like ?, !, zzz
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.font = '40px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    const symbols = {
        question_mark: '?',
        exclamation: '!',
        zzz: 'z',
        musical_notes: '\u266A',
    };
    ctx.fillText(symbols[effectType] || '?', 32, 32);

    const texture = new THREE.CanvasTexture(canvas);
    const mat = new THREE.SpriteMaterial({ map: texture, transparent: true, opacity: 1 });
    const sprite = new THREE.Sprite(mat);
    sprite.position.copy(position);
    sprite.position.y += 1.8;
    sprite.scale.setScalar(0.5);
    sprite.userData.startTime = performance.now();
    sprite.userData.startY = sprite.position.y;
    scene.add(sprite);
    effectSprites.push(sprite);
}

function updateParticleSprites(sprites, duration) {
    const now = performance.now();
    for (let i = sprites.length - 1; i >= 0; i--) {
        const sp = sprites[i];
        const elapsed = now - sp.userData.startTime;

        if (elapsed >= duration) {
            scene.remove(sp);
            if (sp.geometry) sp.geometry.dispose();
            sp.material.dispose();
            sprites.splice(i, 1);
        } else {
            const progress = elapsed / duration;
            sp.position.y = sp.userData.startY + progress * 1.5;
            if (sp.userData.vx) sp.position.x += sp.userData.vx;
            if (sp.userData.vz) sp.position.z += sp.userData.vz;
            sp.material.opacity = 1 - progress * progress;
            if (sp.userData.wobble !== undefined) {
                sp.position.x += Math.sin(now * 0.005 + sp.userData.wobble) * 0.003;
            }
            const baseScale = sp.scale.x < 1 ? 0.7 : 1;
            sp.scale.setScalar(baseScale * (1 + Math.sin(progress * Math.PI) * 0.3));
        }
    }
}

// ---------------------------------------------------------------------------
// Scene setup
// ---------------------------------------------------------------------------

function init() {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!gl) {
        document.getElementById('canvas-container').innerHTML =
            '<div style="color:#fff;text-align:center;padding:50px;font-size:18px;">' +
            'WebGL is not supported in your browser.' +
            '</div>';
        return;
    }

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x1a1a2e);

    const aspect = window.innerWidth / window.innerHeight;
    const frustumSize = 18;
    camera = new THREE.OrthographicCamera(
        frustumSize * aspect / -2,
        frustumSize * aspect / 2,
        frustumSize / 2,
        frustumSize / -2,
        0.1,
        1000
    );
    camera.position.set(20, 20, 20);
    camera.lookAt(10, 0, 7);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    document.getElementById('canvas-container').appendChild(renderer.domElement);

    const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
    scene.add(ambientLight);

    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(10, 20, 10);
    scene.add(dirLight);

    createGround();
    createPets();
    createWindow();
    createPerchPlatforms();
    createPerchMarkers();

    setupEventListeners();
    connectWebSocket();

    animate();
}

function createGround() {
    const geometry = new THREE.PlaneGeometry(20, 14);
    const material = new THREE.MeshStandardMaterial({
        color: 0x2d3436,
        roughness: 0.8
    });
    groundMesh = new THREE.Mesh(geometry, material);
    groundMesh.rotation.x = -Math.PI / 2;
    groundMesh.position.set(10, 0, 7);
    scene.add(groundMesh);

    const gridHelper = new THREE.GridHelper(20, 20, 0x444444, 0x333333);
    gridHelper.position.set(10, 0.01, 7);
    scene.add(gridHelper);

    gridPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
}

function createPets() {
    dogMesh = createDog();
    dogMesh.position.set(3, 0, 3);
    scene.add(dogMesh);

    catMesh = createCat();
    catMesh.position.set(15, 0, 8);
    scene.add(catMesh);
}

function createDog() {
    const dog = new THREE.Group();
    const bodyMat = new THREE.MeshStandardMaterial({ color: 0xd4a574 });
    const darkMat = new THREE.MeshStandardMaterial({ color: 0x8b6914 });
    const noseMat = new THREE.MeshStandardMaterial({ color: 0x2d2d2d });
    const eyeMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a });
    const tongueMat = new THREE.MeshStandardMaterial({ color: 0xff6b8a });

    const bodyGeo = new THREE.CapsuleGeometry(0.35, 0.5, 8, 16);
    const body = new THREE.Mesh(bodyGeo, bodyMat);
    body.rotation.x = Math.PI / 2;
    body.position.set(0, 0.4, 0);
    body.name = 'body';
    dog.add(body);

    const headGeo = new THREE.SphereGeometry(0.32, 16, 16);
    const head = new THREE.Mesh(headGeo, bodyMat);
    head.position.set(0, 0.55, 0.45);
    head.name = 'head';
    dog.add(head);

    const snoutGeo = new THREE.CapsuleGeometry(0.12, 0.15, 8, 8);
    const snout = new THREE.Mesh(snoutGeo, bodyMat);
    snout.rotation.x = Math.PI / 2;
    snout.position.set(0, 0.48, 0.7);
    dog.add(snout);

    const noseGeo = new THREE.SphereGeometry(0.06, 8, 8);
    const nose = new THREE.Mesh(noseGeo, noseMat);
    nose.position.set(0, 0.5, 0.82);
    dog.add(nose);

    const eyeGeo = new THREE.SphereGeometry(0.06, 8, 8);
    const leftEye = new THREE.Mesh(eyeGeo, eyeMat);
    leftEye.position.set(-0.12, 0.62, 0.65);
    dog.add(leftEye);
    const rightEye = new THREE.Mesh(eyeGeo, eyeMat);
    rightEye.position.set(0.12, 0.62, 0.65);
    dog.add(rightEye);

    const eyeShineGeo = new THREE.SphereGeometry(0.02, 8, 8);
    const eyeShineMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    const leftShine = new THREE.Mesh(eyeShineGeo, eyeShineMat);
    leftShine.position.set(-0.1, 0.64, 0.69);
    dog.add(leftShine);
    const rightShine = new THREE.Mesh(eyeShineGeo, eyeShineMat);
    rightShine.position.set(0.14, 0.64, 0.69);
    dog.add(rightShine);

    const earGeo = new THREE.CapsuleGeometry(0.08, 0.2, 4, 8);
    const leftEar = new THREE.Mesh(earGeo, darkMat);
    leftEar.rotation.z = 0.3;
    leftEar.rotation.x = -0.2;
    leftEar.position.set(-0.25, 0.7, 0.35);
    leftEar.name = 'earLeft';
    dog.add(leftEar);
    const rightEar = new THREE.Mesh(earGeo, darkMat);
    rightEar.rotation.z = -0.3;
    rightEar.rotation.x = -0.2;
    rightEar.position.set(0.25, 0.7, 0.35);
    rightEar.name = 'earRight';
    dog.add(rightEar);

    const tailGeo = new THREE.CapsuleGeometry(0.06, 0.25, 4, 8);
    const tail = new THREE.Mesh(tailGeo, bodyMat);
    tail.rotation.x = -0.8;
    tail.rotation.z = 0.2;
    tail.position.set(0, 0.5, -0.45);
    tail.name = 'tail';
    dog.add(tail);

    const legGeo = new THREE.CapsuleGeometry(0.06, 0.15, 4, 8);
    const legNames = ['legFrontLeft', 'legFrontRight', 'legBackLeft', 'legBackRight'];
    const legPositions = [[-0.15, 0.12, 0.2], [0.15, 0.12, 0.2], [-0.15, 0.12, -0.2], [0.15, 0.12, -0.2]];
    legPositions.forEach((pos, i) => {
        const leg = new THREE.Mesh(legGeo, bodyMat);
        leg.position.set(...pos);
        leg.name = legNames[i];
        dog.add(leg);
    });

    const tongueGeo = new THREE.BoxGeometry(0.06, 0.02, 0.12);
    const tongue = new THREE.Mesh(tongueGeo, tongueMat);
    tongue.position.set(0, 0.4, 0.78);
    tongue.name = 'tongue';
    dog.add(tongue);

    return dog;
}

function createCat() {
    const cat = new THREE.Group();
    const bodyMat = new THREE.MeshStandardMaterial({ color: 0x808080 });
    const lightMat = new THREE.MeshStandardMaterial({ color: 0xc0c0c0 });
    const noseMat = new THREE.MeshStandardMaterial({ color: 0xffb6c1 });
    const eyeMat = new THREE.MeshStandardMaterial({ color: 0x90EE90 });
    const pupilMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a });

    const bodyGeo = new THREE.CapsuleGeometry(0.25, 0.4, 8, 16);
    const body = new THREE.Mesh(bodyGeo, bodyMat);
    body.rotation.x = Math.PI / 2;
    body.position.set(0, 0.3, 0);
    body.name = 'body';
    cat.add(body);

    const headGeo = new THREE.SphereGeometry(0.25, 16, 16);
    const head = new THREE.Mesh(headGeo, bodyMat);
    head.scale.set(1, 0.9, 0.95);
    head.position.set(0, 0.45, 0.35);
    head.name = 'head';
    cat.add(head);

    const earGeo = new THREE.ConeGeometry(0.1, 0.18, 4);
    const leftEar = new THREE.Mesh(earGeo, bodyMat);
    leftEar.rotation.z = 0.25;
    leftEar.rotation.x = 0.1;
    leftEar.position.set(-0.15, 0.68, 0.32);
    leftEar.name = 'earLeft';
    cat.add(leftEar);
    const rightEar = new THREE.Mesh(earGeo, bodyMat);
    rightEar.rotation.z = -0.25;
    rightEar.rotation.x = 0.1;
    rightEar.position.set(0.15, 0.68, 0.32);
    rightEar.name = 'earRight';
    cat.add(rightEar);

    const innerEarGeo = new THREE.ConeGeometry(0.05, 0.1, 4);
    const innerEarMat = new THREE.MeshStandardMaterial({ color: 0xffb6c1 });
    const leftInnerEar = new THREE.Mesh(innerEarGeo, innerEarMat);
    leftInnerEar.rotation.z = 0.25;
    leftInnerEar.rotation.x = 0.1;
    leftInnerEar.position.set(-0.15, 0.65, 0.34);
    cat.add(leftInnerEar);
    const rightInnerEar = new THREE.Mesh(innerEarGeo, innerEarMat);
    rightInnerEar.rotation.z = -0.25;
    rightInnerEar.rotation.x = 0.1;
    rightInnerEar.position.set(0.15, 0.65, 0.34);
    cat.add(rightInnerEar);

    const eGeo = new THREE.SphereGeometry(0.06, 12, 12);
    const leftEye = new THREE.Mesh(eGeo, eyeMat);
    leftEye.position.set(-0.1, 0.5, 0.52);
    cat.add(leftEye);
    const rightEye = new THREE.Mesh(eGeo, eyeMat);
    rightEye.position.set(0.1, 0.5, 0.52);
    cat.add(rightEye);

    const pupilGeo = new THREE.SphereGeometry(0.025, 8, 8);
    const leftPupil = new THREE.Mesh(pupilGeo, pupilMat);
    leftPupil.scale.set(0.6, 1.5, 1);
    leftPupil.position.set(-0.1, 0.5, 0.57);
    cat.add(leftPupil);
    const rightPupil = new THREE.Mesh(pupilGeo, pupilMat);
    rightPupil.scale.set(0.6, 1.5, 1);
    rightPupil.position.set(0.1, 0.5, 0.57);
    cat.add(rightPupil);

    const noseGeo = new THREE.SphereGeometry(0.035, 8, 8);
    const nose = new THREE.Mesh(noseGeo, noseMat);
    nose.scale.set(1.2, 0.8, 0.8);
    nose.position.set(0, 0.42, 0.56);
    cat.add(nose);

    const muzzleGeo = new THREE.SphereGeometry(0.06, 8, 8);
    const leftMuzzle = new THREE.Mesh(muzzleGeo, lightMat);
    leftMuzzle.scale.set(1, 0.6, 0.8);
    leftMuzzle.position.set(-0.05, 0.38, 0.54);
    cat.add(leftMuzzle);
    const rightMuzzle = new THREE.Mesh(muzzleGeo, lightMat);
    rightMuzzle.scale.set(1, 0.6, 0.8);
    rightMuzzle.position.set(0.05, 0.38, 0.54);
    cat.add(rightMuzzle);

    const tailGeo = new THREE.CapsuleGeometry(0.04, 0.5, 4, 8);
    const tail = new THREE.Mesh(tailGeo, bodyMat);
    tail.rotation.x = -1.2;
    tail.position.set(0, 0.45, -0.45);
    tail.name = 'tail';
    cat.add(tail);

    const legGeo = new THREE.CapsuleGeometry(0.045, 0.12, 4, 8);
    const legNames = ['legFrontLeft', 'legFrontRight', 'legBackLeft', 'legBackRight'];
    const legPositions = [[-0.1, 0.1, 0.15], [0.1, 0.1, 0.15], [-0.1, 0.1, -0.15], [0.1, 0.1, -0.15]];
    legPositions.forEach((pos, i) => {
        const leg = new THREE.Mesh(legGeo, bodyMat);
        leg.position.set(...pos);
        leg.name = legNames[i];
        cat.add(leg);
    });

    return cat;
}

function createWindow() {
    const windowGeo = new THREE.PlaneGeometry(4, 3);
    const windowMat = new THREE.MeshStandardMaterial({
        color: 0x87ceeb,
        transparent: true,
        opacity: 0.6,
        side: THREE.DoubleSide
    });
    windowMesh = new THREE.Mesh(windowGeo, windowMat);
    windowMesh.position.set(10, 1.5, 0);
    scene.add(windowMesh);
}

function createPerchPlatforms() {
    for (const perch of PERCH_POINTS) {
        const platformGeo = new THREE.CylinderGeometry(0.8, 0.9, perch.height, 16);
        const platformMat = new THREE.MeshStandardMaterial({
            color: 0x8b7355,
            roughness: 0.8
        });
        const platform = new THREE.Mesh(platformGeo, platformMat);
        platform.position.set(perch.x, perch.height / 2, perch.y);
        scene.add(platform);

        const cushionGeo = new THREE.CylinderGeometry(0.7, 0.7, 0.1, 16);
        const cushionMat = new THREE.MeshStandardMaterial({
            color: 0xc9a86c,
            roughness: 0.6
        });
        const cushion = new THREE.Mesh(cushionGeo, cushionMat);
        cushion.position.set(perch.x, perch.height + 0.05, perch.y);
        scene.add(cushion);

        perchPlatforms.push({ platform, cushion, height: perch.height });
    }
}

function createPerchMarkers() {
    const ringGeo = new THREE.RingGeometry(0.6, 0.8, 32);
    const ringMat = new THREE.MeshBasicMaterial({
        color: 0x00bcd4,
        transparent: true,
        opacity: 0.7,
        side: THREE.DoubleSide
    });

    for (const perch of PERCH_POINTS) {
        const ring = new THREE.Mesh(ringGeo, ringMat.clone());
        ring.rotation.x = -Math.PI / 2;
        ring.position.set(perch.x, perch.height + 0.12, perch.y);
        ring.visible = false;
        scene.add(ring);
        perchMarkers.push(ring);
    }
}

function createStimulusMesh(kind) {
    if (kind === 'ball') {
        const geo = new THREE.SphereGeometry(0.25, 16, 16);
        const mat = new THREE.MeshStandardMaterial({ color: 0xff6b6b });
        return new THREE.Mesh(geo, mat);
    } else {
        const geo = new THREE.BoxGeometry(0.35, 0.2, 0.35);
        const mat = new THREE.MeshStandardMaterial({ color: 0x4ecdc4 });
        return new THREE.Mesh(geo, mat);
    }
}

function createScentCircle() {
    const geo = new THREE.CircleGeometry(1, 32);
    const mat = new THREE.MeshBasicMaterial({
        color: 0xffaa00,
        transparent: true,
        opacity: 0.3,
        side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.visible = false;
    scene.add(mesh);
    return mesh;
}

function createPulseMesh() {
    const geo = new THREE.RingGeometry(0.1, 0.3, 32);
    const mat = new THREE.MeshBasicMaterial({
        color: 0x00ffff,
        transparent: true,
        opacity: 0.8,
        side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.visible = false;
    scene.add(mesh);
    return mesh;
}

// ---------------------------------------------------------------------------
// State update from server
// ---------------------------------------------------------------------------

function updateStimuli(state) {
    if (!state || !state.stimuli || !scene) return;

    const currentIds = new Set(state.stimuli.map(s => s.id));

    for (const [id, mesh] of stimulusMeshes) {
        if (!currentIds.has(id)) {
            scene.remove(mesh);
            stimulusMeshes.delete(id);
            delete prevStimulusState[id];
            if (stimThoughtTimers[id]) {
                clearTimeout(stimThoughtTimers[id]);
                delete stimThoughtTimers[id];
            }
        }
    }

    for (const s of state.stimuli) {
        let mesh = stimulusMeshes.get(s.id);
        if (!mesh) {
            mesh = createStimulusMesh(s.kind);
            stimulusMeshes.set(s.id, mesh);
            scene.add(mesh);
        }
        mesh.position.set(s.x, s.kind === 'ball' ? 0.25 : 0.1, s.y);
        const fade = Math.max(0.3, 1 - s.age / 8);
        mesh.material.opacity = fade;
        mesh.material.transparent = true;

        // Render stimulus effects (from BEAR brain)
        const prev = prevStimulusState[s.id] || {};
        if (s.effect && s.effect !== prev.effect) {
            if (s.effect === 'sparkles') {
                showSparkles(mesh.position);
            } else if (s.effect === 'hearts') {
                showPetFeedback(mesh.position);
            } else if (['question_mark', 'exclamation', 'zzz', 'musical_notes'].includes(s.effect)) {
                showEffectBubble(mesh.position, s.effect);
            }
        }

        // Render stimulus thought bubbles
        if (s.thought && s.thought !== prev.thought) {
            showStimulusThought(s.id, mesh, s.thought);
        }

        prevStimulusState[s.id] = { effect: s.effect || null, thought: s.thought || null };
    }
}

function updateWindow(state) {
    if (!state || !state.objects || !windowMesh) return;
    const isOpen = state.objects.window_1.open;
    windowMesh.material.opacity = isOpen ? 0.3 : 0.6;
    windowMesh.material.color.setHex(isOpen ? 0xaaddff : 0x87ceeb);
}

function updateHappinessUI(state) {
    if (!state || !state.pets) return;

    for (const [petId, prefix] of [['dog1', 'dog'], ['cat1', 'cat']]) {
        const pet = state.pets[petId];
        if (!pet) continue;

        const happiness = Math.round(pet.happiness || 0);
        const bar = document.getElementById(`${prefix}-happiness`);
        const value = document.getElementById(`${prefix}-happiness-value`);
        const moodEl = document.getElementById(`${prefix}-mood`);

        if (bar) {
            bar.style.width = `${happiness}%`;
            if (happiness < 30) {
                bar.style.background = 'linear-gradient(90deg, #ff6b6b, #ee5a5a)';
            } else if (happiness < 60) {
                bar.style.background = 'linear-gradient(90deg, #ffd93d, #f9c80e)';
            } else {
                bar.style.background = 'linear-gradient(90deg, #4ecdc4, #44ff88)';
            }
        }
        if (value) value.textContent = `${happiness}%`;
        if (moodEl) moodEl.textContent = pet.mood || '';
    }
}

function updateEffects(state) {
    if (!state || !state.pets) return;

    for (const [petId, mesh] of [['dog1', dogMesh], ['cat1', catMesh]]) {
        const pet = state.pets[petId];
        if (!pet || !mesh) continue;

        // Handle one-shot effects from BEAR
        const effect = pet.effect;
        if (effect && effect !== (prevPetState[petId] || {}).effect) {
            if (effect === 'hearts') {
                showPetFeedback(mesh.position);
            } else if (effect === 'sparkles') {
                showSparkles(mesh.position);
            } else if (['question_mark', 'exclamation', 'zzz', 'musical_notes'].includes(effect)) {
                showEffectBubble(mesh.position, effect);
            }
        }

        // Handle evolved behavior sparkle indicator
        if (pet.is_evolved && !(prevPetState[petId] || {}).is_evolved) {
            showSparkles(mesh.position);
            showEvolutionNotification();
        }

        // Handle thought bubbles
        const thought = pet.thought;
        if (thought && thought !== (prevPetState[petId] || {}).thought) {
            showThoughtBubble(petId, mesh, thought);
        }

        prevPetState[petId] = { effect: pet.effect, is_evolved: pet.is_evolved, thought: pet.thought };
    }
}

function showThoughtBubble(petId, mesh, text) {
    const el = document.getElementById(`thought-bubble-${petId === 'dog1' ? 'dog' : 'cat'}`);
    if (!el) return;

    el.textContent = text;
    el.classList.add('visible');

    // Clear previous timer
    if (thoughtTimers[petId]) clearTimeout(thoughtTimers[petId]);
    thoughtTimers[petId] = setTimeout(() => {
        el.classList.remove('visible');
    }, 3000);
}

function showStimulusThought(stimId, mesh, text) {
    // Reuse a floating effect bubble to show stimulus thought
    // Create a small canvas-based sprite above the stimulus
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgba(0,0,0,0.6)';
    ctx.beginPath();
    ctx.roundRect(2, 2, 252, 60, 8);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = '20px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    // Truncate if needed
    const displayText = text.length > 30 ? text.substring(0, 27) + '...' : text;
    ctx.fillText(displayText, 128, 32);

    const texture = new THREE.CanvasTexture(canvas);
    const mat = new THREE.SpriteMaterial({ map: texture, transparent: true, opacity: 1 });
    const sprite = new THREE.Sprite(mat);
    sprite.position.copy(mesh.position);
    sprite.position.y += 1.0;
    sprite.scale.set(2, 0.5, 1);
    sprite.userData.startTime = performance.now();
    sprite.userData.startY = sprite.position.y;
    scene.add(sprite);
    effectSprites.push(sprite);
}

function updateThoughtBubblePositions() {
    // Project 3D pet positions to screen for thought bubble placement
    if (!camera || !renderer) return;

    for (const [petId, mesh] of [['dog1', dogMesh], ['cat1', catMesh]]) {
        if (!mesh) continue;
        const el = document.getElementById(`thought-bubble-${petId === 'dog1' ? 'dog' : 'cat'}`);
        if (!el || !el.classList.contains('visible')) continue;

        const pos = mesh.position.clone();
        pos.y += 2;
        pos.project(camera);

        const x = (pos.x * 0.5 + 0.5) * window.innerWidth;
        const y = (-pos.y * 0.5 + 0.5) * window.innerHeight;

        el.style.left = `${x - 50}px`;
        el.style.top = `${y - 40}px`;
    }
}

function showEvolutionNotification() {
    const el = document.getElementById('evolution-notification');
    if (!el) return;
    el.classList.add('visible');
    setTimeout(() => el.classList.remove('visible'), 3000);
}

// ---------------------------------------------------------------------------
// Overlays
// ---------------------------------------------------------------------------

function updateDogOverlay(state) {
    if (!state || !state.stimuli || !state.pets) return;

    const isDogMode = VIEW_MODES[viewMode] === 'Dog';

    for (const [id, circle] of scentCircles) {
        if (!isDogMode) circle.visible = false;
    }

    if (!isDogMode) {
        if (scentLine) scentLine.visible = false;
        return;
    }

    const currentIds = new Set(state.stimuli.map(s => s.id));
    for (const [id, circle] of scentCircles) {
        if (!currentIds.has(id)) {
            scene.remove(circle);
            scentCircles.delete(id);
        }
    }

    let bestStim = null;
    let bestScore = -Infinity;
    const dogX = state.pets.dog1.x;
    const dogY = state.pets.dog1.y;

    for (const s of state.stimuli) {
        let circle = scentCircles.get(s.id);
        if (!circle) {
            circle = createScentCircle();
            scentCircles.set(s.id, circle);
        }

        const fresh = Math.max(0, Math.min(1, 1 - s.age / 8));
        const kindBoost = s.kind === 'ball' ? 1.3 : 1.0;
        const strength = fresh * kindBoost;

        circle.position.set(s.x, 0.015, s.y);
        circle.scale.setScalar(1 + strength * 2);
        circle.material.opacity = 0.15 + strength * 0.3;
        circle.visible = true;

        const dist = Math.sqrt((s.x - dogX) ** 2 + (s.y - dogY) ** 2);
        const score = strength / (dist + 1);
        if (score > bestScore) {
            bestScore = score;
            bestStim = s;
        }
    }

    if (bestStim) {
        if (!scentLine) {
            const mat = new THREE.LineBasicMaterial({
                color: 0xffaa00,
                transparent: true,
                opacity: 0.5
            });
            const geo = new THREE.BufferGeometry();
            scentLine = new THREE.Line(geo, mat);
            scene.add(scentLine);
        }
        const points = [
            new THREE.Vector3(dogX, 0.1, dogY),
            new THREE.Vector3(bestStim.x, 0.1, bestStim.y)
        ];
        scentLine.geometry.setFromPoints(points);
        scentLine.visible = true;
    } else if (scentLine) {
        scentLine.visible = false;
    }
}

function updateCatOverlay(state) {
    if (!state || !state.stimuli || !state.pets) return;

    const isCatMode = VIEW_MODES[viewMode] === 'Cat';

    for (const marker of perchMarkers) {
        marker.visible = isCatMode;
    }

    const now = performance.now();
    if (isCatMode) {
        for (const s of state.stimuli) {
            if (!prevStimulusIds.has(s.id)) {
                addMotionPulse(s.x, s.y, now);
            }
        }

        for (const petId of ['dog1', 'cat1']) {
            const pet = state.pets[petId];
            const prev = prevPetPositions[petId];
            if (prev) {
                const dist = Math.sqrt((pet.x - prev.x) ** 2 + (pet.y - prev.y) ** 2);
                if (dist > 0.5) {
                    addMotionPulse(pet.x, pet.y, now);
                }
            }
        }
    }

    for (let i = motionPulses.length - 1; i >= 0; i--) {
        const pulse = motionPulses[i];
        const elapsed = (now - pulse.startTime) / 1000;
        if (elapsed > 0.6) {
            pulse.mesh.visible = false;
            pulsePool.push(pulse.mesh);
            motionPulses.splice(i, 1);
        } else if (isCatMode) {
            const t = elapsed / 0.6;
            pulse.mesh.scale.setScalar(1 + t * 3);
            pulse.mesh.material.opacity = 0.8 * (1 - t);
            pulse.mesh.visible = true;
        } else {
            pulse.mesh.visible = false;
        }
    }

    prevStimulusIds = new Set(state.stimuli.map(s => s.id));
    for (const petId of ['dog1', 'cat1']) {
        const pet = state.pets[petId];
        prevPetPositions[petId] = { x: pet.x, y: pet.y };
    }
}

function addMotionPulse(x, y, startTime) {
    let mesh = pulsePool.pop();
    if (!mesh) {
        mesh = createPulseMesh();
    }
    mesh.position.set(x, 0.03, y);
    mesh.scale.setScalar(1);
    mesh.material.opacity = 0.8;
    mesh.visible = true;
    motionPulses.push({ mesh, startTime, x, y });
}

// ---------------------------------------------------------------------------
// Pet position interpolation and animation
// ---------------------------------------------------------------------------

function lerp(a, b, t) {
    return a + (b - a) * t;
}

function updatePetPositions() {
    if (!nextSnapshot || !dogMesh || !catMesh) return;

    const now = performance.now();
    let alpha = (now - snapshotReceivedTime) / SNAPSHOT_INTERVAL;
    alpha = Math.max(0, Math.min(1, alpha));

    const state = nextSnapshot.state;
    if (!state || !state.pets) return;

    const dogNext = state.pets.dog1;
    const catNext = state.pets.cat1;
    if (!dogNext || !catNext) return;

    const dogHeight = dogNext.z || 0;
    const catHeight = catNext.z || 0;

    if (prevSnapshot && prevSnapshot.state && prevSnapshot.state.pets) {
        const prevState = prevSnapshot.state;

        const dogPrev = prevState.pets.dog1;
        if (dogPrev) {
            const dogDist = Math.sqrt((dogNext.x - dogPrev.x) ** 2 + (dogNext.y - dogPrev.y) ** 2);
            let dogX, dogZ;
            if (dogDist > 2) {
                dogX = dogNext.x;
                dogZ = dogNext.y;
            } else {
                dogX = lerp(dogPrev.x, dogNext.x, alpha);
                dogZ = lerp(dogPrev.y, dogNext.y, alpha);
            }
            const prevDogHeight = dogPrev.z || 0;
            const interpDogHeight = lerp(prevDogHeight, dogHeight, alpha);
            dogMesh.position.set(dogX, interpDogHeight, dogZ);
        }

        if (dogNext.vx !== 0 || dogNext.vy !== 0) {
            const angle = Math.atan2(dogNext.vx, dogNext.vy);
            dogMesh.rotation.y = angle;
        }

        const catPrev = prevState.pets.cat1;
        if (catPrev) {
            const catDist = Math.sqrt((catNext.x - catPrev.x) ** 2 + (catNext.y - catPrev.y) ** 2);
            let catX, catZ;
            if (catDist > 2) {
                catX = catNext.x;
                catZ = catNext.y;
            } else {
                catX = lerp(catPrev.x, catNext.x, alpha);
                catZ = lerp(catPrev.y, catNext.y, alpha);
            }
            const prevCatHeight = catPrev.z || 0;
            const interpCatHeight = lerp(prevCatHeight, catHeight, alpha);
            catMesh.position.set(catX, interpCatHeight, catZ);
        }

        if (catNext.vx !== 0 || catNext.vy !== 0) {
            const angle = Math.atan2(catNext.vx, catNext.vy);
            catMesh.rotation.y = angle;
        }
    } else {
        dogMesh.position.set(dogNext.x, dogHeight, dogNext.y);
        catMesh.position.set(catNext.x, catHeight, catNext.y);
    }
}

function animatePets(time) {
    if (dogMesh) {
        const dogState = nextSnapshot?.state?.pets?.dog1;
        const dogAnim = dogState?.animation || 'idle';
        const dogMood = dogState?.mood || 'neutral';

        const dogTail = dogMesh.getObjectByName('tail');
        if (dogTail) {
            let tailSpeed = 12;
            let tailSwing = 0.4;

            if (dogAnim === 'sit') {
                tailSpeed = 4; tailSwing = 0.2;
            } else if (dogAnim === 'nuzzle') {
                tailSpeed = 30; tailSwing = 0.8;
            } else if (dogAnim === 'excited_bounce' || dogMood === 'excited') {
                tailSpeed = 25; tailSwing = 0.7;
            } else if (dogAnim === 'play_bow' || dogMood === 'playful') {
                tailSpeed = 20; tailSwing = 0.6;
            } else if (dogMood === 'sleepy') {
                tailSpeed = 4; tailSwing = 0.15;
            } else if (dogAnim === 'back_away' || dogMood === 'cautious') {
                tailSpeed = 3; tailSwing = 0.1;
            }

            dogTail.rotation.z = Math.sin(time * tailSpeed) * tailSwing + 0.2;
            dogTail.rotation.y = dogAnim === 'nuzzle' ? Math.sin(time * 15) * 0.3 : 0;
        }

        const dogTongue = dogMesh.getObjectByName('tongue');
        if (dogTongue) {
            const tongueExtend = (dogAnim === 'nuzzle' || dogMood === 'excited') ? 0.05 : 0.02;
            dogTongue.position.z = 0.78 + Math.sin(time * 3) * tongueExtend;
            if (dogAnim === 'nuzzle') {
                dogTongue.scale.y = 1 + Math.sin(time * 8) * 0.2;
            }
        }

        // Animation-specific body movements
        if (dogAnim === 'nuzzle') {
            const bounce = Math.abs(Math.sin(time * 10)) * 0.2;
            dogMesh.position.y = (dogState.z || 0) + bounce;
            dogMesh.rotation.z = Math.sin(time * 6) * 0.08;
            const dogHead = dogMesh.getObjectByName('head');
            if (dogHead) {
                dogHead.rotation.x = Math.sin(time * 4) * 0.15 - 0.1;
            }
        } else if (dogAnim === 'excited_bounce') {
            const bounce = Math.abs(Math.sin(time * 12)) * 0.15;
            dogMesh.position.y = (dogState?.z || 0) + bounce;
            dogMesh.rotation.z = Math.sin(time * 8) * 0.05;
        } else if (dogAnim === 'play_bow') {
            // Front dips down, rear stays up
            dogMesh.rotation.x = Math.sin(time * 3) * 0.1 + 0.15;
            dogMesh.rotation.z = 0;
        } else if (dogAnim === 'head_tilt') {
            dogMesh.rotation.z = Math.sin(time * 2) * 0.12;
            const dogHead = dogMesh.getObjectByName('head');
            if (dogHead) dogHead.rotation.z = 0.2;
        } else if (dogAnim === 'sit') {
            // Rear tilts down, front stays upright — no big Y drop
            dogMesh.rotation.x = -0.4;
            dogMesh.rotation.z = 0;
            dogMesh.position.y = (dogState?.z || 0) - 0.08;
            const dogHead = dogMesh.getObjectByName('head');
            if (dogHead) dogHead.rotation.x = 0.3;
        } else if (dogAnim === 'back_away') {
            dogMesh.rotation.z = 0;
            dogMesh.rotation.x = -0.05;
        } else {
            dogMesh.rotation.z = 0;
            dogMesh.rotation.x = 0;
        }

        // Ear animation based on mood
        const earL = dogMesh.getObjectByName('earLeft');
        const earR = dogMesh.getObjectByName('earRight');
        if (earL && earR) {
            if (dogMood === 'cautious' || dogMood === 'annoyed') {
                earL.rotation.x = -0.5; earR.rotation.x = -0.5;
            } else if (dogMood === 'excited' || dogMood === 'playful') {
                earL.rotation.x = 0.1 + Math.sin(time * 6) * 0.1;
                earR.rotation.x = 0.1 + Math.sin(time * 6) * 0.1;
            } else {
                earL.rotation.x = -0.2; earR.rotation.x = -0.2;
            }
        }

        // Walking
        const dogIsMoving = dogState && (Math.abs(dogState.vx) > 0.1 || Math.abs(dogState.vy) > 0.1);
        const speedMod = dogState?.speed_modifier || 1;
        const dogWalkSpeed = dogIsMoving ? 15 * speedMod : 0;
        const dogSwing = Math.sin(time * dogWalkSpeed) * 0.4;

        const dogFL = dogMesh.getObjectByName('legFrontLeft');
        const dogFR = dogMesh.getObjectByName('legFrontRight');
        const dogBL = dogMesh.getObjectByName('legBackLeft');
        const dogBR = dogMesh.getObjectByName('legBackRight');
        if (dogAnim === 'sit') {
            // Front legs straight, back legs tucked
            if (dogFL) dogFL.rotation.x = 0.1;
            if (dogFR) dogFR.rotation.x = 0.1;
            if (dogBL) dogBL.rotation.x = 0.8;
            if (dogBR) dogBR.rotation.x = 0.8;
        } else {
            if (dogFL) dogFL.rotation.x = dogSwing;
            if (dogFR) dogFR.rotation.x = -dogSwing;
            if (dogBL) dogBL.rotation.x = -dogSwing;
            if (dogBR) dogBR.rotation.x = dogSwing;
        }
    }

    if (catMesh) {
        const catState = nextSnapshot?.state?.pets?.cat1;
        const catAnim = catState?.animation || 'idle';
        const catMood = catState?.mood || 'neutral';

        const catTail = catMesh.getObjectByName('tail');
        if (catTail) {
            let tailSpeed = 2;
            let tailSwing = 0.15;

            if (catAnim === 'sit') {
                tailSpeed = 1.5; tailSwing = 0.1;
            } else if (catAnim === 'knead') {
                tailSpeed = 8; tailSwing = 0.4;
            } else if (catMood === 'annoyed') {
                tailSpeed = 12; tailSwing = 0.5; // aggressive flicking
            } else if (catMood === 'playful') {
                tailSpeed = 6; tailSwing = 0.3;
            } else if (catAnim === 'pounce_ready') {
                tailSpeed = 15; tailSwing = 0.2; // rapid twitching
            } else if (catMood === 'sleepy') {
                tailSpeed = 1; tailSwing = 0.08;
            }

            catTail.rotation.z = Math.sin(time * tailSpeed) * tailSwing;
            catTail.rotation.x = catAnim === 'knead' ? 0.3 + Math.sin(time * 3) * 0.1 : 0;
        }

        if (catAnim === 'knead') {
            const purr = Math.sin(time * 6);
            catMesh.scale.y = 1 + purr * 0.12;
            catMesh.scale.x = 1 - purr * 0.06;
            catMesh.scale.z = 1 + purr * 0.06;
            const archBack = Math.sin(time * 2) * 0.1 + 0.1;
            catMesh.position.y = (catState.z || 0) + archBack;
            catMesh.rotation.x = -0.15;
            catMesh.rotation.z = Math.sin(time * 1.5) * 0.08;
            const catHead = catMesh.getObjectByName('head');
            if (catHead) {
                catHead.rotation.z = Math.sin(time * 2) * 0.15;
                catHead.rotation.x = -0.2;
                catHead.position.y = 0.45 + Math.sin(time * 3) * 0.03;
            }
            // Kneading paws
            const catFL = catMesh.getObjectByName('legFrontLeft');
            const catFR = catMesh.getObjectByName('legFrontRight');
            if (catFL) catFL.rotation.x = Math.sin(time * 4) * 0.2;
            if (catFR) catFR.rotation.x = Math.sin(time * 4 + Math.PI) * 0.2;
        } else if (catAnim === 'pounce_ready') {
            // Low crouch, wiggling rear
            catMesh.scale.set(1, 0.85, 1.1);
            catMesh.rotation.x = 0.1;
            catMesh.rotation.z = Math.sin(time * 10) * 0.03; // wiggle
        } else if (catAnim === 'stretch') {
            catMesh.scale.set(0.95, 1.1, 1.15);
            catMesh.rotation.x = -0.1;
        } else if (catAnim === 'swat_playful') {
            const catFL = catMesh.getObjectByName('legFrontLeft');
            if (catFL) catFL.rotation.z = Math.sin(time * 15) * 0.5;
            catMesh.scale.set(1, 1, 1);
            catMesh.rotation.z = 0;
            catMesh.rotation.x = 0;
        } else if (catAnim === 'cautious_approach') {
            catMesh.scale.set(1, 0.9, 1.05);
            catMesh.rotation.x = 0.05;
        } else if (catAnim === 'sit') {
            // Upright, compact sit — clearly lowered
            catMesh.scale.set(1, 0.85, 1);
            catMesh.rotation.x = -0.3;
            catMesh.rotation.z = 0;
            catMesh.position.y = (catState?.z || 0) - 0.25;
        } else if (catAnim === 'head_tilt') {
            catMesh.scale.set(1, 1, 1);
            catMesh.rotation.z = Math.sin(time * 2) * 0.1;
            const catHead = catMesh.getObjectByName('head');
            if (catHead) catHead.rotation.z = 0.25;
        } else {
            catMesh.scale.set(1, 1, 1);
            catMesh.rotation.z = 0;
            catMesh.rotation.x = 0;
        }

        // Ear animation based on mood
        const earL = catMesh.getObjectByName('earLeft');
        const earR = catMesh.getObjectByName('earRight');
        if (earL && earR) {
            if (catMood === 'annoyed') {
                earL.rotation.z = 0.6; earR.rotation.z = -0.6; // flattened
            } else if (catMood === 'cautious') {
                earL.rotation.z = 0.4; earR.rotation.z = -0.4;
            } else if (catMood === 'excited' || catMood === 'playful') {
                earL.rotation.z = 0.15 + Math.sin(time * 4) * 0.1;
                earR.rotation.z = -0.15 - Math.sin(time * 4) * 0.1;
            } else {
                earL.rotation.z = 0.25; earR.rotation.z = -0.25;
            }
        }

        // Walking
        const catIsMoving = catState && (Math.abs(catState.vx) > 0.1 || Math.abs(catState.vy) > 0.1);
        const catSpeedMod = catState?.speed_modifier || 1;
        const catWalkSpeed = catIsMoving ? 10 * catSpeedMod : 0;
        const catSwing = Math.sin(time * catWalkSpeed) * 0.3;

        if (catAnim !== 'knead' && catAnim !== 'sit') {
            const catFL = catMesh.getObjectByName('legFrontLeft');
            const catFR = catMesh.getObjectByName('legFrontRight');
            const catBL = catMesh.getObjectByName('legBackLeft');
            const catBR = catMesh.getObjectByName('legBackRight');
            if (catFL) catFL.rotation.x = catSwing;
            if (catFR) catFR.rotation.x = -catSwing;
            if (catBL) catBL.rotation.x = -catSwing;
            if (catBR) catBR.rotation.x = catSwing;
        }
    }
}

// ---------------------------------------------------------------------------
// Camera controls
// ---------------------------------------------------------------------------

function updateCameraZoom() {
    const aspect = window.innerWidth / window.innerHeight;
    const frustumSize = 18 / cameraZoom;
    camera.left = frustumSize * aspect / -2;
    camera.right = frustumSize * aspect / 2;
    camera.top = frustumSize / 2;
    camera.bottom = frustumSize / -2;
    camera.updateProjectionMatrix();
}

function updateCameraPosition() {
    const baseX = 20 + cameraOffset.x;
    const baseZ = 20 + cameraOffset.z;
    camera.position.set(baseX, 20, baseZ);
    camera.lookAt(10 + cameraOffset.x, 0, 7 + cameraOffset.z);
}

function getTouchDistance(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.sqrt(dx * dx + dy * dy);
}

function getTouchCenter(touches) {
    return {
        x: (touches[0].clientX + touches[1].clientX) / 2,
        y: (touches[0].clientY + touches[1].clientY) / 2
    };
}

function handleTouchStart(e) {
    if (e.touches.length === 2) {
        e.preventDefault();
        touchState.active = true;
        touchState.isPanning = true;
        touchState.isZooming = true;
        touchState.lastTouches = Array.from(e.touches).map(t => ({
            clientX: t.clientX,
            clientY: t.clientY
        }));
    }
}

function handleTouchMove(e) {
    if (!touchState.active || e.touches.length !== 2) return;
    e.preventDefault();

    const currentTouches = Array.from(e.touches).map(t => ({
        clientX: t.clientX,
        clientY: t.clientY
    }));

    if (touchState.isZooming) {
        const lastDist = getTouchDistance(touchState.lastTouches);
        const currentDist = getTouchDistance(currentTouches);
        const zoomDelta = (currentDist - lastDist) * 0.005;
        cameraZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, cameraZoom + zoomDelta));
        updateCameraZoom();
    }

    if (touchState.isPanning) {
        const lastCenter = getTouchCenter(touchState.lastTouches);
        const currentCenter = getTouchCenter(currentTouches);
        const panScale = 0.03 / cameraZoom;
        cameraOffset.x -= (currentCenter.x - lastCenter.x) * panScale;
        cameraOffset.z -= (currentCenter.y - lastCenter.y) * panScale;
        cameraOffset.x = Math.max(-10, Math.min(10, cameraOffset.x));
        cameraOffset.z = Math.max(-7, Math.min(7, cameraOffset.z));
        updateCameraPosition();
    }

    touchState.lastTouches = currentTouches;
}

function handleTouchEnd(e) {
    if (e.touches.length < 2) {
        touchState.active = false;
        touchState.isPanning = false;
        touchState.isZooming = false;
    }

    if (e.touches.length === 0 && e.changedTouches.length === 1 && !touchState.active) {
        const touch = e.changedTouches[0];
        handleCanvasClick({ clientX: touch.clientX, clientY: touch.clientY });
    }
}

function handleWheel(e) {
    e.preventDefault();
    const zoomDelta = -e.deltaY * 0.001;
    cameraZoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, cameraZoom + zoomDelta));
    updateCameraZoom();
}

// ---------------------------------------------------------------------------
// Input handling
// ---------------------------------------------------------------------------

function setupEventListeners() {
    window.addEventListener('resize', () => {
        updateCameraZoom();
        renderer.setSize(window.innerWidth, window.innerHeight);
    });

    const canvas = renderer.domElement;
    canvas.style.cursor = 'pointer';
    canvas.addEventListener('pointerdown', handleCanvasClick);

    canvas.addEventListener('touchstart', handleTouchStart, { passive: false });
    canvas.addEventListener('touchmove', handleTouchMove, { passive: false });
    canvas.addEventListener('touchend', handleTouchEnd, { passive: false });
    canvas.addEventListener('wheel', handleWheel, { passive: false });

    document.getElementById('btn-ball').addEventListener('click', () => setTool('ball'));
    document.getElementById('btn-treat').addEventListener('click', () => setTool('treat'));
    document.getElementById('btn-window').addEventListener('click', () => {
        sendAction({ type: 'action', action: 'toggle_window' });
    });
    document.getElementById('btn-view').addEventListener('click', cycleViewMode);
    document.getElementById('btn-brain').addEventListener('click', toggleBrainPanel);
    document.getElementById('btn-learned').addEventListener('click', toggleLearnedPanel);
    document.getElementById('teach-btn').addEventListener('click', sendTeach);
    document.getElementById('say-btn').addEventListener('click', sendCommand);
    document.getElementById('teach-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.shiftKey) {
            e.preventDefault();
            sendCommand();
        } else if (e.key === 'Enter') {
            sendTeach();
        }
    });
}

function setTool(tool) {
    currentTool = tool;
    document.querySelectorAll('.action-btn').forEach(btn => btn.classList.remove('selected'));
    document.getElementById(`btn-${tool}`).classList.add('selected');
}

function cycleViewMode() {
    viewMode = (viewMode + 1) % VIEW_MODES.length;
    const label = VIEW_MODES[viewMode];
    document.getElementById('btn-view').textContent = `View: ${label}`;

    const modeLabel = document.getElementById('view-mode-label');
    modeLabel.textContent = `${label} View`;
    modeLabel.classList.add('visible');
    setTimeout(() => modeLabel.classList.remove('visible'), 2000);
}

// ---------------------------------------------------------------------------
// Brain Decision Log Panel
// ---------------------------------------------------------------------------

function toggleBrainPanel() {
    brainPanelOpen = !brainPanelOpen;
    document.getElementById('brain-panel').classList.toggle('open', brainPanelOpen);
    document.getElementById('btn-brain').classList.toggle('active', brainPanelOpen);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function updateBrainPanel(snapshot) {
    if (!snapshot.brain_stats || !snapshot.brain_stats.decisions) return;

    const decisions = snapshot.brain_stats.decisions;
    const body = document.getElementById('brain-panel-body');
    const badge = document.getElementById('brain-panel-badge');

    let newCount = 0;

    for (const d of decisions) {
        if (d.seq <= lastDecisionSeq) continue;
        lastDecisionSeq = d.seq;
        newCount++;
        decisionCount++;

        let petClass, petIcon, petLabel;
        if (d.entity_type === 'stimulus') {
            petClass = 'stimulus';
            petIcon = d.pet_id.includes('ball') || d.query?.includes('ball') ? '\u26BD' : '\uD83C\uDF6A';
            petLabel = d.pet_id;
        } else if (d.pet_id === 'dog1') {
            petClass = 'dog';
            petIcon = '\uD83D\uDC15';
            petLabel = 'Dog';
        } else {
            petClass = 'cat';
            petIcon = '\uD83D\uDC08';
            petLabel = 'Cat';
        }
        const timeStr = new Date(d.ts * 1000).toLocaleTimeString();

        const entry = document.createElement('div');
        entry.className = `brain-entry ${petClass}`;

        let html = `
            <div class="brain-entry-header">
                <span class="brain-entry-pet">${petIcon} ${petLabel}</span>
                <span class="brain-entry-time">${timeStr}</span>
            </div>
            <div class="brain-entry-trigger">${escapeHtml(d.trigger)}</div>
            <div class="brain-entry-query">${escapeHtml(d.query)}</div>
        `;

        // Retrieved instruction IDs
        if (d.retrieved && d.retrieved.length > 0) {
            html += `<details class="brain-detail"><summary>Retrieved (${d.retrieved.length})</summary>`;
            html += `<pre class="brain-pre">${d.retrieved.map(escapeHtml).join('\n')}</pre></details>`;
        }

        // System prompt (LLM input — includes composed guidance)
        if (d.system_prompt) {
            html += `<details class="brain-detail"><summary>System Prompt</summary>`;
            html += `<pre class="brain-pre">${escapeHtml(d.system_prompt)}</pre></details>`;
        }

        // Situation (LLM user message)
        if (d.situation) {
            html += `<details class="brain-detail"><summary>Situation (User Msg)</summary>`;
            html += `<pre class="brain-pre">${escapeHtml(d.situation)}</pre></details>`;
        }

        // LLM response or fallback
        if (d.is_fallback) {
            const reason = d.llm_response ? escapeHtml(d.llm_response) : 'No LLM';
            html += `<div class="brain-entry-fallback">Fallback: ${reason}</div>`;
        } else if (d.llm_response) {
            html += `<details class="brain-detail" open><summary>LLM Response</summary>`;
            html += `<pre class="brain-pre">${escapeHtml(d.llm_response)}</pre></details>`;
        }

        const a = d.action;
        if (a) {
            let tags = '';
            if (a.mood) tags += `<span class="brain-tag">mood: ${a.mood}</span>`;
            if (a.animation) tags += `<span class="brain-tag">anim: ${a.animation}</span>`;
            if (a.speed && a.speed !== 'normal') tags += `<span class="brain-tag">speed: ${a.speed}</span>`;
            if (a.happiness_delta) tags += `<span class="brain-tag">happy: ${a.happiness_delta > 0 ? '+' : ''}${a.happiness_delta}</span>`;
            if (a.intent) tags += `<span class="brain-tag">${escapeHtml(a.intent)}</span>`;
            if (a.is_evolved) tags += `<span class="brain-tag evolved">evolved</span>`;
            if (a.is_taught) tags += `<span class="brain-tag taught">taught</span>`;
            if (tags) html += `<div class="brain-entry-action">${tags}</div>`;
        }

        entry.innerHTML = html;
        body.appendChild(entry);
    }

    // Prune old DOM entries
    while (body.children.length > 20) {
        body.removeChild(body.firstChild);
    }

    badge.textContent = decisionCount;

    if (newCount > 0) {
        body.scrollTop = body.scrollHeight;
    }
}

// ---------------------------------------------------------------------------
// Teach / Learned Panel
// ---------------------------------------------------------------------------

function sendTeach() {
    if (teachPending) return;
    const input = document.getElementById('teach-input');
    const content = input.value.trim();
    if (!content) return;

    const petId = document.getElementById('teach-pet').value;
    const btn = document.getElementById('teach-btn');

    teachPending = true;
    btn.disabled = true;
    btn.textContent = 'Teaching...';

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'action',
            action: 'teach',
            pet_id: petId,
            content: content,
        }));
    }

    // Timeout fallback: re-enable after 20s
    setTimeout(() => {
        if (teachPending) {
            teachPending = false;
            btn.disabled = false;
            btn.textContent = 'Teach';
        }
    }, 20000);
}

function handleTaughtMessage(msg) {
    const btn = document.getElementById('teach-btn');
    const input = document.getElementById('teach-input');
    teachPending = false;
    btn.disabled = false;
    btn.textContent = 'Teach';
    input.value = '';

    const petIcon = msg.species === 'dog' ? '\uD83D\uDC15' : '\uD83D\uDC08';
    const petName = msg.species === 'dog' ? 'Buddy' : 'Whiskers';

    // Add to learned behaviors
    learnedBehaviors.push({
        id: msg.instruction.id,
        pet_id: msg.pet_id,
        species: msg.species,
        content: msg.instruction.content,
        tags: msg.instruction.tags || [],
        scope: msg.instruction.scope || [],
        user_message: msg.user_message,
        ts: Date.now(),
        triggerCount: 0,
    });

    // Show toast
    const toast = document.getElementById('teach-toast');
    const toastText = document.getElementById('teach-toast-text');
    toastText.textContent = `${petName} learned: "${msg.user_message}"`;
    toast.classList.add('visible');
    setTimeout(() => toast.classList.remove('visible'), 4000);

    // Update learned panel
    updateLearnedPanel();

    // Update badge
    document.getElementById('learned-panel-badge').textContent = learnedBehaviors.length;
}

function handleTeachError(msg) {
    const btn = document.getElementById('teach-btn');
    teachPending = false;
    btn.disabled = false;
    btn.textContent = 'Teach';

    const toast = document.getElementById('teach-toast');
    const toastText = document.getElementById('teach-toast-text');
    toastText.textContent = `Teaching failed: ${msg.message || 'unknown error'}`;
    toast.classList.add('visible');
    setTimeout(() => toast.classList.remove('visible'), 3000);
}

function sendCommand() {
    const input = document.getElementById('teach-input');
    const content = input.value.trim();
    if (!content) return;

    const petId = document.getElementById('teach-pet').value;
    const btn = document.getElementById('say-btn');

    btn.disabled = true;
    btn.textContent = 'Sending...';

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'action',
            action: 'command',
            pet_id: petId,
            content: content,
        }));
    }

    // Re-enable after short delay (commands are fast, no LLM wait)
    setTimeout(() => {
        btn.disabled = false;
        btn.textContent = 'Say';
    }, 1000);
}

function handleCommandAck(msg) {
    const btn = document.getElementById('say-btn');
    btn.disabled = false;
    btn.textContent = 'Say';
    document.getElementById('teach-input').value = '';
}

function toggleLearnedPanel() {
    learnedPanelOpen = !learnedPanelOpen;
    document.getElementById('learned-panel').classList.toggle('open', learnedPanelOpen);
    document.getElementById('btn-learned').classList.toggle('active', learnedPanelOpen);
}

function updateLearnedPanel() {
    const body = document.getElementById('learned-panel-body');

    if (learnedBehaviors.length === 0) {
        body.innerHTML = '<div class="learned-empty">No behaviors taught yet. Use the teach bar below!</div>';
        return;
    }

    body.innerHTML = '';
    for (const b of [...learnedBehaviors].reverse()) {
        const petIcon = b.species === 'dog' ? '\uD83D\uDC15' : '\uD83D\uDC08';
        const petName = b.species === 'dog' ? 'Dog' : 'Cat';
        const timeStr = new Date(b.ts).toLocaleTimeString();

        const entry = document.createElement('div');
        entry.className = 'learned-entry';
        entry.id = `learned-${b.id}`;

        let tagsHtml = '';
        for (const tag of (b.scope || [])) {
            tagsHtml += `<span class="brain-tag">${escapeHtml(tag)}</span>`;
        }

        entry.innerHTML = `
            <div class="learned-entry-header">
                <span class="learned-entry-pet">${petIcon} ${petName}</span>
                <span class="learned-entry-time">${timeStr}</span>
            </div>
            <div class="learned-entry-user-msg">"${escapeHtml(b.user_message)}"</div>
            <div class="learned-entry-content">${escapeHtml(b.content)}</div>
            <div class="learned-entry-tags">${tagsHtml}</div>
            <div class="learned-entry-triggered" id="triggered-${b.id}">
                Triggered ${b.triggerCount} time${b.triggerCount !== 1 ? 's' : ''}
            </div>
        `;
        body.appendChild(entry);
    }
}

function checkTaughtTriggers(snapshot) {
    if (!snapshot.brain_stats || !snapshot.brain_stats.decisions) return;
    for (const d of snapshot.brain_stats.decisions) {
        if (d.seq <= lastDecisionSeq) continue;
        if (d.action && d.action.is_taught) {
            // Find which learned behavior was triggered
            for (const b of learnedBehaviors) {
                // Check if any retrieved instruction ID matches a learned behavior
                if (d.retrieved && d.retrieved.includes(b.id)) {
                    b.triggerCount++;
                    // Flash the learned entry
                    const el = document.getElementById(`triggered-${b.id}`);
                    if (el) {
                        el.textContent = `Triggered ${b.triggerCount} time${b.triggerCount !== 1 ? 's' : ''}`;
                        el.classList.add('active');
                        setTimeout(() => el.classList.remove('active'), 3000);
                    }
                    // Show the evolution notification for taught behavior
                    const evoNotif = document.getElementById('evolution-notification');
                    const evoText = document.getElementById('evo-text');
                    const petName = d.pet_id === 'dog1' ? 'Buddy' : 'Whiskers';
                    evoText.textContent = `${petName} used a taught behavior!`;
                    evoNotif.classList.add('visible');
                    setTimeout(() => evoNotif.classList.remove('visible'), 3000);
                    break;
                }
            }
        }
    }
}

function checkLearningEvents(snapshot) {
    if (!snapshot.brain_stats || !snapshot.brain_stats.learning_events) return;
    for (const evt of snapshot.brain_stats.learning_events) {
        if (evt.type === 'evolved') {
            const evoNotif = document.getElementById('evolution-notification');
            const evoText = document.getElementById('evo-text');
            evoText.textContent = `Pet figured something out! (${evt.count} new behavior${evt.count > 1 ? 's' : ''})`;
            evoNotif.classList.add('visible');
            setTimeout(() => evoNotif.classList.remove('visible'), 4000);
        }
    }
}

function handleCanvasClick(event) {
    if (!renderer || !camera || !gridPlane) return;

    const rect = renderer.domElement.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(new THREE.Vector2(x, y), camera);

    const petMeshes = [];
    if (dogMesh) petMeshes.push({ mesh: dogMesh, id: 'dog1' });
    if (catMesh) petMeshes.push({ mesh: catMesh, id: 'cat1' });

    for (const { mesh, id } of petMeshes) {
        const intersects = raycaster.intersectObject(mesh, true);
        if (intersects.length > 0) {
            sendAction({ type: 'action', action: 'pet', pet_id: id });
            // Hearts/effects now come from BEAR brain via [!effect(hearts)]
            return;
        }
    }

    const intersection = new THREE.Vector3();
    if (raycaster.ray.intersectPlane(gridPlane, intersection)) {
        const gridX = Math.floor(intersection.x);
        const gridY = Math.floor(intersection.z);

        if (gridX >= 0 && gridX < 20 && gridY >= 0 && gridY < 14) {
            if (currentTool === 'ball') {
                sendAction({ type: 'action', action: 'drop_ball', x: gridX, y: gridY });
            } else if (currentTool === 'treat') {
                sendAction({ type: 'action', action: 'place_treat', x: gridX, y: gridY });
            }
        }
    }
}

function sendAction(action) {
    const now = Date.now();
    if (now - lastActionTime < ACTION_COOLDOWN) return;
    lastActionTime = now;

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(action));
    }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

function connectWebSocket() {
    const params = new URLSearchParams(window.location.search);
    const roomId = params.get('room') || 'lobby';
    document.getElementById('room-name').textContent = roomId;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/${roomId}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        connected = true;
        reconnectDelay = 1000;
        lastDecisionSeq = 0;
        updateConnectionStatus(true);
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'snapshot') {
                handleSnapshot(msg);
            } else if (msg.type === 'taught') {
                handleTaughtMessage(msg);
            } else if (msg.type === 'teach_error') {
                handleTeachError(msg);
            } else if (msg.type === 'command_ack') {
                handleCommandAck(msg);
            } else if (msg.type === 'error' && msg.message === 'rate_limited') {
                showRateLimitWarning();
            }
        } catch (e) {
            console.error('Error parsing message:', e);
        }
    };

    ws.onclose = () => {
        connected = false;
        updateConnectionStatus(false);
        scheduleReconnect();
    };

    ws.onerror = () => {
        ws.close();
    };
}

function handleSnapshot(snapshot) {
    prevSnapshot = nextSnapshot;
    nextSnapshot = snapshot;
    snapshotReceivedTime = performance.now();

    if (!snapshot || !snapshot.state) return;

    const state = snapshot.state;
    updateStimuli(state);
    updateWindow(state);
    updateHappinessUI(state);
    updateEffects(state);
    updateDogOverlay(state);
    updateCatOverlay(state);
    checkTaughtTriggers(snapshot);
    checkLearningEvents(snapshot);
    updateBrainPanel(snapshot);

    document.getElementById('player-count').textContent = snapshot.players || 0;
}

function updateConnectionStatus(isConnected) {
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    if (isConnected) {
        dot.classList.add('connected');
        text.textContent = 'Connected';
    } else {
        dot.classList.remove('connected');
        text.textContent = 'Disconnected';
    }
}

function showRateLimitWarning() {
    const warning = document.getElementById('rate-limit-warning');
    warning.classList.add('visible');
    setTimeout(() => warning.classList.remove('visible'), 1500);
}

function scheduleReconnect() {
    setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 1.5, 8000);
        connectWebSocket();
    }, reconnectDelay);
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

function animate(time) {
    requestAnimationFrame(animate);
    updatePetPositions();
    animatePets(time / 1000);
    updateParticleSprites(heartSprites, 1500);
    updateParticleSprites(sparkleSprites, 1200);
    updateParticleSprites(effectSprites, 2000);
    updateThoughtBubblePositions();
    renderer.render(scene, camera);
}

init();
