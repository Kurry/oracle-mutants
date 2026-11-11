import { readFileSync } from 'node:fs';
import babel from '@babel/core';
import { parse } from '@babel/parser';
import { Instrumenter } from '@stryker-mutator/instrumenter';

const version = '8.7.1';
const logger = { debug() {}, info() {}, warn() {}, error() {}, isDebugEnabled: () => false };

function sourceFile({ path, source, ranges }) {
  const starts = [0];
  const ends = [];
  for (const newline of source.matchAll(/\r\n|[\n\r\u2028\u2029]/g)) {
    ends.push(newline.index);
    starts.push(newline.index + newline[0].length);
  }
  ends.push(source.length);

  const mutate = ranges.map(([start, end]) => {
    if (!Number.isInteger(start) || !Number.isInteger(end) ||
        start < 1 || end < start || end > starts.length) {
      throw new Error(`Invalid mutation line range for ${path}: ${start}-${end}`);
    }
    return {
      start: { line: start - 1, column: 0 },
      end: { line: end - 1, column: ends[end - 1] - starts[end - 1] },
    };
  });
  const offset = ({ line, column }) => {
    if (starts[line] === undefined || column < 0 || starts[line] + column > ends[line]) {
      throw new Error(`Invalid mutation position for ${path}: ${line + 1}:${column + 1}`);
    }
    return starts[line] + column;
  };
  return {
    name: path,
    content: source,
    mutate,
    offset,
    contains: (start, end) =>
      start < end && mutate.some((range) => start >= offset(range.start) && end <= offset(range.end)),
  };
}

function assignmentName(node) {
  if (node.type === 'Identifier') return node.name;
  if (node.type === 'ThisExpression') return 'this';
  if (node.type === 'MemberExpression' && !node.computed && node.property.type === 'Identifier') {
    const receiver = assignmentName(node.object);
    if (receiver) return `${receiver}.${node.property.name}`;
  }
  return null;
}

function rawInputMutation(file, assignment) {
  const plugins = ['decorators-legacy'];
  if (/\.[cm]?tsx?$/.test(file.name)) plugins.push('typescript');
  if (/\.(?:[cm]?jsx?|tsx)$/.test(file.name)) plugins.push('jsx');
  const ast = parse(file.content, { sourceType: 'unambiguous', plugins });
  const targets = [];
  babel.traverse(ast, {
    AssignmentExpression(path) {
      const { left, right, operator } = path.node;
      if (operator === '=' && left.type === 'MemberExpression' &&
          assignmentName(left) === assignment && file.contains(right.start, right.end)) {
        targets.push(path);
      }
    },
  });
  if (targets.length !== 1) {
    throw new Error(`Expected one changed assignment to ${assignment} in ${file.name}, found ${targets.length}`);
  }
  const [target] = targets;
  const { right } = target.node;
  const binding = right.type === 'Identifier' && target.scope.getBinding(right.name);
  const fallback = binding?.path.node.init;
  if (!binding?.constant || binding.path.getFunctionParent() !== target.getFunctionParent() ||
      fallback?.type !== 'ConditionalExpression' || fallback.end > right.start ||
      !assignmentName(fallback.alternate)) {
    throw new Error(`Expected a local conditional fallback before assignment to ${assignment} in ${file.name}`);
  }
  const raw = fallback.alternate;
  const replacement = file.content.slice(raw.start, raw.end);
  const root = assignmentName(raw).split('.')[0];
  if (root !== 'this' && target.scope.getBinding(root) !== binding.path.scope.getBinding(root)) {
    throw new Error(`Raw input ${root} is shadowed at assignment to ${assignment} in ${file.name}`);
  }
  return mutation(file, right.start, right.end, replacement, 'UseRawInputAtDisplayAssignment');
}

function mutation(file, start, end, replacement, operator) {
  return {
    path: file.name,
    start: Buffer.byteLength(file.content.slice(0, start), 'utf8'),
    end: Buffer.byteLength(file.content.slice(0, end), 'utf8'),
    replacement,
    operator,
  };
}

async function generate({ files, recipes = [] }) {
  const sources = files.map(sourceFile);
  const byPath = new Map(sources.map((file) => [file.name, file]));
  const { mutants } = await new Instrumenter(logger).instrument(sources, {
    plugins: null, excludedMutations: [], ignorers: [],
  });
  const mutations = [];
  for (const mutant of mutants) {
    const file = byPath.get(mutant.fileName);
    if (!file) throw new Error(`Unexpected mutation file: ${mutant.fileName}`);
    const start = file.offset(mutant.location.start);
    const end = file.offset(mutant.location.end);
    if (file.contains(start, end) && file.content.slice(start, end) !== mutant.replacement) {
      mutations.push(mutation(file, start, end, mutant.replacement, mutant.mutatorName));
    }
  }
  for (const { path, assignment } of recipes) {
    const file = byPath.get(path);
    if (!file) throw new Error(`Recipe requires changed JS or TS source: ${path}`);
    mutations.push(rawInputMutation(file, assignment));
  }
  return { version, mutations };
}

try {
  const request = JSON.parse(readFileSync(0, 'utf8'));
  console.log(JSON.stringify(await generate(request)));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
