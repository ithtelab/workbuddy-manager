/**
 * 上游任务码 → 人能懂的名字（issue #189）。
 *
 * 任务码来自上游 `task_runner.py` 的 TASKS 注册表（`Sequential_Tasks_2`、
 * `first_buddy` 这类内部标识），用户看不出那是「小程序里选中专家并完成对话」。
 * 这里只做**展示层**的对应关系：码本身（数据）照旧出现在原文日志里，
 * 认不出的码原样显示 —— 上游随时会加新任务，不能因为不认识就吞掉一行。
 *
 * 为什么不从上游读名字：脚本没有机器可读的「码 → 名称」输出（名字只写在它自己的
 * 注释里）。等上游加了结构化输出再换，届时删掉这张表即可。
 */
import {t as tStatic} from './index';

/** 任务码 → 短语表 key。键名用 `tasks.taskName_*`，五套语言都有译文。 */
const NAME_KEY: Record<string, string> = {
  // 桌面端任务
  skill_1: 'tasks.taskName_skill1',
  automation_1: 'tasks.taskName_automation1',
  playbook_prompt: 'tasks.taskName_playbook',
  Expert_lighthouse: 'tasks.taskName_expertLighthouse',
  Hp_Appearance: 'tasks.taskName_appearance',
  chat_5: 'tasks.taskName_chat5',
  'Model_chat_GLM5.2': 'tasks.taskName_modelGlm',
  black_cat: 'tasks.taskName_blackCat',
  first_buddy: 'tasks.taskName_firstBuddy',
  RichMeow_Chat: 'tasks.taskName_richMeow',
  'Buddy_App/_QQ': 'tasks.taskName_buddyApp',
  Library_read: 'tasks.taskName_library',
  // 小程序 / 开学季
  chat_3_times: 'tasks.taskName_chat3',
  expert_use: 'tasks.taskName_schoolExpert',
  share_invite: 'tasks.taskName_shareInvite',
  task_student_verify: 'tasks.taskName_studentVerify',
  desktop_chat_1_time: 'tasks.taskName_desktopChat',
  Sequential_Tasks_1: 'tasks.taskName_seq1',
  Sequential_Tasks_2: 'tasks.taskName_seq2',
  Sequential_Tasks_3: 'tasks.taskName_seq3',
  Sequential_Tasks_4: 'tasks.taskName_seq4',
  Sequential_Tasks_5: 'tasks.taskName_seq5',
  school_season: 'tasks.taskName_schoolSeason',
};

/** 任务码 → 当前语言的名字；不认识返回原码。 */
export function taskName(code: string): string {
  const key = NAME_KEY[code];
  return key ? tStatic(key) : code;
}
