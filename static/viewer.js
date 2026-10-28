import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

let renderer, scene, camera, controls, mesh, raf, ro;
const box = document.getElementById("viewer");
const loading = document.getElementById("viewerLoading");

function init() {
  if (renderer) return;
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  box.appendChild(renderer.domElement);
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(40, 1, 0.1, 5000);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x38618c, 1.1));
  const d = new THREE.DirectionalLight(0xffffff, 1.4); d.position.set(1, 2, 1.5); scene.add(d);
  const d2 = new THREE.DirectionalLight(0x35a7ff, 0.6); d2.position.set(-2, -1, -1); scene.add(d2);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.autoRotate = true; controls.autoRotateSpeed = 2;
  controls.addEventListener("start", () => (controls.autoRotate = false));
  ro = new ResizeObserver(resize); ro.observe(box);
}
function resize() {
  const w = box.clientWidth, h = box.clientHeight || 400;
  renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
}
function loop() { raf = requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); }

window.show3D = function (url) {
  init(); resize();
  loading.hidden = false; loading.textContent = "Loading model…";
  if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); mesh = null; }
  new STLLoader().load(url, (geo) => {
    geo.computeVertexNormals(); geo.center();
    geo.rotateX(-Math.PI / 2); // STL Z-up -> three Y-up
    mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0xff5964, metalness: 0.1, roughness: 0.55 }));
    scene.add(mesh);
    geo.computeBoundingSphere();
    const r = geo.boundingSphere.radius || 50;
    camera.position.set(r * 1.6, r * 1.2, r * 2.2); camera.near = r / 100; camera.far = r * 100;
    camera.updateProjectionMatrix(); controls.target.set(0, 0, 0); controls.autoRotate = true; controls.update();
    loading.hidden = true;
  }, undefined, () => { loading.textContent = "Couldn't load the 3D preview — use Download STL."; });
  cancelAnimationFrame(raf); loop();
};
window.stop3D = () => cancelAnimationFrame(raf);
