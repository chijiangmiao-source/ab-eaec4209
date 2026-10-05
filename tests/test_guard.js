// 前端单调修订号守卫的验收测试（node 直接运行）。
// 模拟“计算期间继续录入”：较晚到达的旧修订响应不得覆盖已显示的更新页面。
const assert = require('assert');
const path = require('path');
const { createRevisionGuard } = require(path.join(__dirname, '..', 'app', 'static', 'app.js'));

let failed = 0;
function check(name, fn) {
  try { fn(); console.log('  ✓ ' + name); }
  catch (e) { failed += 1; console.error('  ✗ ' + name + '\n    ' + e.message); }
}

check('接受首个投影并记录修订号', () => {
  const g = createRevisionGuard(-1);
  assert.strictEqual(g.accept({ revision: 3 }), true);
  assert.strictEqual(g.displayed, 3);
});

check('同修订号重复响应允许（幂等轮询）', () => {
  const g = createRevisionGuard(-1);
  g.accept({ revision: 5 });
  assert.strictEqual(g.accept({ revision: 5 }), true);
  assert.strictEqual(g.suppressed, 0);
});

check('旧修订响应被抑制且不推进已显示修订号', () => {
  const g = createRevisionGuard(-1);
  g.accept({ revision: 5 });
  g.accept({ revision: 8 });
  assert.strictEqual(g.accept({ revision: 4 }), false);  // 计算期间到达的旧结果
  assert.strictEqual(g.accept({ revision: 7 }), false);
  assert.strictEqual(g.displayed, 8);                    // 最新页面仍是 rev 8
  assert.strictEqual(g.suppressed, 2);
});

check('旧结果抑制在交错到达序列中持续有效', () => {
  const g = createRevisionGuard(-1);
  // 模拟：页面先拿到 rev 6，计算期间乱序到达 rev 4、rev 9、rev 6
  assert.strictEqual(g.accept({ revision: 6 }), true);
  assert.strictEqual(g.accept({ revision: 4 }), false);
  assert.strictEqual(g.accept({ revision: 9 }), true);
  assert.strictEqual(g.accept({ revision: 6 }), false);
  assert.strictEqual(g.displayed, 9);
});

check('守卫不修改传入的状态对象', () => {
  const g = createRevisionGuard(-1);
  const s = { revision: 2 };
  g.accept(s);
  assert.deepStrictEqual(s, { revision: 2 });
});

check('reset 后重新建档的 rev=0 投影可以正常渲染', () => {
  const g = createRevisionGuard(-1);
  g.accept({ revision: 9 });
  assert.strictEqual(g.accept({ revision: 0 }), false);  // 未重置：旧基线会误抑制
  g.reset();
  assert.strictEqual(g.accept({ revision: 0 }), true);   // 重置后：rev=0 是新页面
  assert.strictEqual(g.displayed, 0);
  assert.strictEqual(g.accept({ revision: 1 }), true);
});

if (failed) { console.error(`\n前端守卫测试失败 ${failed} 项`); process.exit(1); }
console.log('\n前端守卫测试全部通过');
