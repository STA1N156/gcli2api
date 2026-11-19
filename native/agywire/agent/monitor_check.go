package agent

import "encoding/json"

type MonitorCheckInput struct {
	Traj []struct {
		Kind    string
		Text    string
		Thought string
		Calls   []struct {
			Name string
			Args json.RawMessage
		}
		Output *string
		Error  *string
	}
	Step, K       int
	Prompt, Model string
	Level         string
}

func RenderMonitorForCheck(in MonitorCheckInput) []byte {
	traj := make([]trajStep, len(in.Traj))
	for i, t := range in.Traj {
		var calls []FunctionCall
		for _, c := range t.Calls {
			calls = append(calls, FunctionCall{Name: c.Name, Args: c.Args})
		}
		switch t.Kind {
		case "user":
			traj[i] = trajStep{Kind: trajUser, Text: t.Text}
		case "model":
			traj[i] = trajStep{Kind: trajModel, Text: t.Text, Thought: t.Thought, Calls: calls}
		default:
			st := trajStep{Kind: trajEnv, Output: t.Output, Error: t.Error}
			if len(calls) > 0 {
				st.Call = calls[0]
			}
			traj[i] = st
		}
	}
	cur := traj[in.Step]
	text := renderMonitorText(renderLatestStepTrajectory(traj, in.Step, in.K, cur), renderPending(cur.Calls[in.K]), in.Prompt)
	return RenderMonitor(text, in.Model, in.Level)
}
