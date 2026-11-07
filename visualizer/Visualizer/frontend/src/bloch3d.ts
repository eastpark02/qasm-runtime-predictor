import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import type {QubitState} from './types';

// The Blender exporter applies this basis change to the static asset exactly once.
// Apply it once to live Qiskit vectors too; camera motion never changes state.
export const quantumToThree = ([x, y, z]: readonly number[]) => [x, z, -y] as const;
const axes = [
  ['+Z |0⟩', [0, 0, 1.24]], ['−Z |1⟩', [0, 0, -1.24]],
  ['+X', [1.24, 0, 0]], ['−X', [-1.24, 0, 0]],
  ['+Y', [0, 1.24, 0]], ['−Y', [0, -1.24, 0]],
] as const;
const views = new Set<View>();
let renderer: THREE.WebGLRenderer | undefined;
let assetPromise: Promise<THREE.Group> | undefined;
let frame = 0;
let unavailable = false;

function shade(material: THREE.MeshStandardMaterial, shell: boolean) {
  material.onBeforeCompile = shader => {
    shader.uniforms.cueColor = {value: new THREE.Color(0x111c24)};
    const limb = shell ? 'float limb=pow(1.0-abs(dot(normalize(vNormal),normalize(vViewPosition))),2.6);gl_FragColor.rgb=mix(gl_FragColor.rgb,vec3(.16,.36,.36),limb*.75);gl_FragColor.a=clamp(gl_FragColor.a+limb*.3,0.,1.);' : '';
    shader.fragmentShader = shader.fragmentShader
      .replace('void main() {', 'uniform vec3 cueColor;\nvoid main() {')
      .replace('#include <opaque_fragment>', `#include <opaque_fragment>\n/*bloch-cue*/${limb}gl_FragColor.rgb=mix(gl_FragColor.rgb,cueColor,.74*smoothstep(-.2,.9,vViewPosition.z+viewMatrix[3].z));`);
    if (!shader.fragmentShader.includes('/*bloch-cue*/')) throw Error('Bloch material shader needs updating');
  };
  material.customProgramCacheKey = () => shell ? 'bloch-matte-shell' : 'bloch-matte';
}

function asset() {
  return assetPromise ??= new GLTFLoader().loadAsync('/assets/bloch-sphere.glb').then(gltf => {
    gltf.scene.traverse(object => {
      if (!(object instanceof THREE.Mesh)) return;
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      for (const material of materials) {
        if (!(material instanceof THREE.MeshStandardMaterial)) continue;
        const shell = material.name === 'Bloch translucent shell';
        shade(material, shell);
        if (shell) {
          object.renderOrder = -1;
          Object.assign(material, {depthWrite: false, transparent: false, side: THREE.BackSide,
            blending: THREE.CustomBlending, blendSrc: THREE.SrcAlphaFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
            blendSrcAlpha: THREE.OneFactor, blendDstAlpha: THREE.OneMinusSrcAlphaFactor});
        }
      }
    });
    return gltf.scene;
  });
}

function fail() {
  unavailable = true;
  for (const view of views) view.mode('fallback');
  renderer?.dispose();
  renderer = undefined;
}

function invalidate(view: View) {
  view.dirty = true;
  if (!frame && !unavailable) frame = requestAnimationFrame(render);
}

// A single offscreen WebGL context serves all cards, including large registers.
// Copy only changed, visible views to local canvases, so DOM labels remain above
// their own sphere and each card retains independent pointer/keyboard controls.
function render() {
  frame = 0;
  if (!renderer || unavailable) return;
  try {
    for (const view of views) {
      if (!view.dirty || !view.ready || !view.visible) continue;
      const rect = view.element.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      const scale = Math.min(devicePixelRatio || 1, 2);
      const width = Math.round(rect.width * scale), height = Math.round(rect.height * scale);
      renderer.setSize(width, height, false);
      view.camera.aspect = width / height;
      view.camera.updateProjectionMatrix();
      renderer.render(view.scene, view.camera);
      if (view.canvas.width !== width) view.canvas.width = width;
      if (view.canvas.height !== height) view.canvas.height = height;
      view.context.clearRect(0, 0, width, height);
      view.context.drawImage(renderer.domElement, 0, 0);
      view.camera.updateMatrixWorld();
      axes.forEach(([, point], i) => {
        const p = new THREE.Vector3(...quantumToThree(point)).project(view.camera);
        view.labels[i].style.left = `${THREE.MathUtils.clamp((p.x * .5 + .5) * rect.width, 24, rect.width - 24)}px`;
        view.labels[i].style.top = `${THREE.MathUtils.clamp((-p.y * .5 + .5) * rect.height, 14, rect.height - 14)}px`;
      });
      view.element.dataset.camera = JSON.stringify(view.camera.position.toArray());
      view.mode('3d');
      view.dirty = false;
    }
  } catch (error) {
    console.info('Bloch 3D unavailable; using exact XZ projection.', error);
    fail();
  }
}

function clearDynamic(group: THREE.Group) {
  group.traverse(object => {
    if (!(object instanceof THREE.Mesh)) return;
    object.geometry.dispose();
    (Array.isArray(object.material) ? object.material : [object.material]).forEach(material => material.dispose());
  });
  group.clear();
}

type Mode = 'loading' | '3d' | 'fallback';
type View = {
  element: HTMLElement; canvas: HTMLCanvasElement; context: CanvasRenderingContext2D;
  scene: THREE.Scene; camera: THREE.PerspectiveCamera; controls: OrbitControls;
  dynamic: THREE.Group; labels: HTMLSpanElement[]; dirty: boolean; ready: boolean;
  visible: boolean; mode: (value: Mode) => void;
};

export function createBlochView(element: HTMLElement, canvas: HTMLCanvasElement, labelsElement: HTMLElement,
  onMode: (mode: Mode) => void) {
  const context = canvas.getContext('2d');
  if (!context) { onMode('fallback'); return {update: (_state: QubitState) => {}, reset: () => {}, dispose: () => {}}; }
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, .1, 30);
  camera.position.set(2.9, 2.1, 3.2);
  scene.add(new THREE.AmbientLight(0xffffff, .28), new THREE.HemisphereLight(0x9cdfe6, 0x172126, 1.5));
  const key = new THREE.DirectionalLight(0xf2fbff, 1.9); key.position.set(2.2, 2.6, 1.2);
  const rim = new THREE.DirectionalLight(0x9ff5e6, 2.2); rim.position.set(-1.6, 1.8, -9);
  const fill = new THREE.DirectionalLight(0xbfe6ee, .7); fill.position.set(-2.4, -1.6, 1.4);
  camera.add(key, rim, fill); scene.add(camera);
  const controls = new OrbitControls(camera, element);
  controls.enablePan = false; controls.enableDamping = false;
  // Scroll continues to navigate the page. Keyboard +/- zooms a focused sphere.
  controls.enableZoom = false; controls.minDistance = 3.6; controls.maxDistance = 7;
  controls.saveState();
  const dynamic = new THREE.Group(); scene.add(dynamic);
  const labels = axes.map(([text]) => {
    const label = document.createElement('span'); label.textContent = text;
    labelsElement.append(label); return label;
  });
  const view: View = {element, canvas, context, scene, camera, controls, dynamic, labels,
    dirty: true, ready: false, visible: true, mode(value) { element.dataset.renderer = value; onMode(value); }};
  views.add(view);
  const changed = () => invalidate(view);
  controls.addEventListener('change', changed);
  const keydown = (event: KeyboardEvent) => {
    if (!['r', 'R', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', '+', '=', '-', '_'].includes(event.key)) return;
    event.preventDefault();
    if (event.key.toLowerCase() === 'r') controls.reset();
    else {
      const p = new THREE.Spherical().setFromVector3(camera.position);
      if (event.key === 'ArrowLeft') p.theta -= .15;
      if (event.key === 'ArrowRight') p.theta += .15;
      if (event.key === 'ArrowUp') p.phi = Math.max(.1, p.phi - .15);
      if (event.key === 'ArrowDown') p.phi = Math.min(Math.PI - .1, p.phi + .15);
      if (['+', '='].includes(event.key)) p.radius = Math.max(3.6, p.radius * .9);
      if (['-', '_'].includes(event.key)) p.radius = Math.min(7, p.radius / .9);
      camera.position.setFromSpherical(p); controls.update();
    }
    invalidate(view);
  };
  element.addEventListener('keydown', keydown);
  const resize = new ResizeObserver(changed); resize.observe(element);
  const intersection = new IntersectionObserver(entries => {
    view.visible = entries[0].isIntersecting;
    if (view.visible) invalidate(view);
  });
  intersection.observe(element);
  if (!unavailable) {
    try {
      if (!renderer) {
        renderer = new THREE.WebGLRenderer({antialias: true, alpha: true, powerPreference: 'low-power'});
        renderer.setPixelRatio(1);
        renderer.domElement.addEventListener('webglcontextlost', event => { event.preventDefault(); fail(); });
      }
      asset().then(model => {
        if (!views.has(view) || unavailable) return;
        scene.add(model.clone(true)); view.ready = true; invalidate(view);
      }).catch(fail);
    } catch { fail(); }
  } else view.mode('fallback');
  return {
    update(state: QubitState) {
      clearDynamic(dynamic);
      const end = new THREE.Vector3(...quantumToThree(state.bloch));
      const length = end.length();
      const zero = length < 1e-12;
      const material = () => new THREE.MeshBasicMaterial({color: 0x81f1c1, toneMapped: false});
      if (!zero) {
        const head = Math.min(.11, length * .3), shaft = length - head;
        const arrow = new THREE.Group();
        arrow.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), end.clone().normalize());
        const stem = new THREE.Mesh(new THREE.CylinderGeometry(.012, .012, shaft, 12), material());
        stem.position.y = shaft / 2; arrow.add(stem);
        const cone = new THREE.Mesh(new THREE.ConeGeometry(Math.min(.043, length * .12), head, 20), material());
        cone.position.y = shaft + head / 2; arrow.add(cone); dynamic.add(arrow);
      }
      const tip = new THREE.Mesh(new THREE.SphereGeometry(zero ? .038 : .018, 18, 12), material());
      tip.position.copy(end); dynamic.add(tip);
      element.dataset.state = JSON.stringify(state);
      element.dataset.tip = JSON.stringify(end.toArray());
      element.dataset.zero = String(zero);
      invalidate(view);
    },
    reset() { controls.reset(); invalidate(view); },
    dispose() {
      views.delete(view); controls.removeEventListener('change', changed); controls.dispose();
      resize.disconnect(); intersection.disconnect(); element.removeEventListener('keydown', keydown);
      labels.forEach(label => label.remove()); clearDynamic(dynamic);
      if (!views.size) {
        if (frame) cancelAnimationFrame(frame); frame = 0;
        renderer?.dispose(); renderer = undefined; unavailable = false;
      }
    },
  };
}
