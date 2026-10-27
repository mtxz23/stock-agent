/** 回归验证旧提问定位后仍能查看后续消息，并保持各分支隔离。 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { questionBranchTail } from '../src/conversation_navigation.ts';

const task = (id, parent_id, order) => ({ id, parent_id, created_at: String(order).padStart(3, '0') });
const chain = [task('root', null, 1), task('second', 'root', 2), task('latest', 'second', 3)];

test('从最新消息定位第一条或中间提问，仍加载最新消息', () => {
  assert.equal(questionBranchTail(chain, 'root', 'latest'), 'latest');
  assert.equal(questionBranchTail(chain, 'second', 'latest'), 'latest');
});

test('从已被截断的旧提问恢复，也带上后续消息', () => {
  assert.equal(questionBranchTail(chain, 'root', 'root'), 'latest');
  assert.equal(questionBranchTail(chain, 'second', 'second'), 'latest');
});

test('定位当前分支的祖先，不会切到更新的其他分支', () => {
  const fork = [...chain, task('branch', 'root', 4), task('branch-latest', 'branch', 5)];
  assert.equal(questionBranchTail(fork, 'root', 'latest'), 'latest');
  assert.equal(questionBranchTail(fork, 'root', 'branch-latest'), 'branch-latest');
});

test('定位其他分支时带上该分支后续消息，不混入原分支', () => {
  const fork = [...chain, task('branch', 'root', 4), task('branch-latest', 'branch', 5)];
  assert.equal(questionBranchTail(fork, 'branch', 'latest'), 'branch-latest');
  assert.equal(questionBranchTail(fork, 'second', 'branch-latest'), 'latest');
});

test('从其他对话定位旧问题，选择目标问题后最新的完整分支', () => {
  const fork = [...chain, task('branch', 'root', 4), task('branch-latest', 'branch', 5), task('other', null, 6)];
  assert.equal(questionBranchTail(fork, 'root', 'other'), 'branch-latest');
});

test('任务输入顺序不影响定位，独立对话不会被串入', () => {
  assert.equal(questionBranchTail([...chain].reverse().concat(task('other', null, 9)), 'root', null), 'latest');
});

test('已删除的目标和循环关联返回明确错误', () => {
  assert.throws(() => questionBranchTail(chain, 'deleted', 'latest'), /已不存在/);
  assert.throws(() => questionBranchTail([task('a', 'b', 1), task('b', 'a', 2)], 'a', null), /关联异常/);
});
