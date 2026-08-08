import { TestBed } from '@angular/core/testing';
import { provideCopilotKit } from '@copilotkit/angular';
import { HttpAgent } from '@ag-ui/client';
import { provideA2Ui, BasicCatalog } from '@a2ui/angular/v0_9';
import { App } from './app';

describe('App', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideCopilotKit({
          agents: { default: new HttpAgent({ url: 'http://localhost:5196' }) },
        }),
        provideA2Ui(() => ({ catalogs: [new BasicCatalog()] })),
      ],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    const app = fixture.componentInstance;
    expect(app).toBeTruthy();
  });
});
