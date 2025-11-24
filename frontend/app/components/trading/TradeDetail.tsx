import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Brain, Terminal, FileJson } from 'lucide-react'

interface TradeDetailProps {
  reasoning?: string
  prompt?: string
  ai_output?: string
}

export default function TradeDetail({ reasoning, prompt, ai_output }: TradeDetailProps) {
  return (
    <div className="p-4 bg-muted/30 rounded-lg border border-border animate-in fade-in slide-in-from-top-2">
      <Tabs defaultValue="reasoning" className="w-full">
        <TabsList className="grid w-full grid-cols-3 mb-4">
          <TabsTrigger value="reasoning" className="flex items-center gap-2">
            <Brain className="w-4 h-4" />
            Reasoning
          </TabsTrigger>
          <TabsTrigger value="prompt" className="flex items-center gap-2">
            <Terminal className="w-4 h-4" />
            Full Prompt
          </TabsTrigger>
          <TabsTrigger value="json" className="flex items-center gap-2">
            <FileJson className="w-4 h-4" />
            JSON Output
          </TabsTrigger>
        </TabsList>

        <TabsContent value="reasoning" className="mt-0">
          <Card>
            <CardHeader className="py-3">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                AI Reasoning Analysis
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="prose prose-sm dark:prose-invert max-w-none">
                <p className="text-sm leading-relaxed whitespace-pre-wrap">
                  {reasoning || "No reasoning data available for this trade."}
                </p>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="prompt" className="mt-0">
          <Card>
            <CardHeader className="py-3">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                System Prompt & Context
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="h-[300px] w-full rounded-md border p-4 bg-muted/50 overflow-auto">
                <pre className="text-xs font-mono whitespace-pre-wrap break-words text-foreground/80">
                  {prompt || "No prompt data available."}
                </pre>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="json" className="mt-0">
          <Card>
            <CardHeader className="py-3">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Raw Model Output
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="h-[300px] w-full rounded-md border p-4 bg-muted/50 overflow-auto">
                <pre className="text-xs font-mono whitespace-pre-wrap break-words text-green-500/90">
                  {ai_output || "No JSON output available."}
                </pre>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
